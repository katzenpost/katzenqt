from __future__ import annotations

import configparser
import importlib
import os
import runpy
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest


def appending_to(target: "list[str]") -> "Callable[..., None]":
    def record(value: object, *rest: object) -> None:
        target.append(str(value))

    return record


@pytest.fixture
def launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[ModuleType]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KATZENQT_KPCLIENTD_TCP", raising=False)
    monkeypatch.delenv("KATZENQT_NO_AUTO_INSTALL", raising=False)
    sys.modules.pop("katzenqt.launcher", None)
    module = importlib.import_module("katzenqt.launcher")
    yield module
    sys.modules.pop("katzenqt.launcher", None)


def test_the_runtime_dir_falls_back_to_a_temp_dir_without_getuid(
    monkeypatch: pytest.MonkeyPatch,
    launcher: ModuleType,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delattr(os, "getuid")
    assert launcher._runtime_dir() == Path(tempfile.gettempdir())


def test_the_runtime_dir_uses_the_uid_when_there_is_no_xdg_dir(
    monkeypatch: pytest.MonkeyPatch,
    launcher: ModuleType,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert launcher._runtime_dir() == Path(f"/run/user/{os.getuid()}")


def test_alive_sees_a_listening_unix_socket(
    launcher: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen(1)
        assert launcher.alive(str(path)) is True
    assert launcher.alive(str(tmp_path / "absent")) is False


def test_thin_refuses_an_unknown_network(launcher: ModuleType) -> None:
    with pytest.raises(ValueError, match="must be Unix or Tcp"):
        launcher.thin("/run/x.sock", "Smoke")


def test_inside_flatpak_the_socket_launch_changes_into_the_app_dir(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", True)
    monkeypatch.setattr(launcher, "endpoint", lambda: str(tmp_path / "sock"))
    monkeypatch.setattr(launcher.subprocess, "call", lambda *_a: 0)
    chdirs: list[str] = []
    monkeypatch.setattr(launcher.os, "chdir", appending_to(chdirs))
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert chdirs == ["/app/share/katzenqt"]


def test_running_the_module_reports_its_status(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["launcher", "--status"])
    sys.modules.pop("katzenqt.launcher", None)
    runpy.run_module("katzenqt.launcher", run_name="__main__")
    assert capsys.readouterr().out.strip() in {
        "native",
        "bundled",
        "unavailable",
    }


def test_tcp_alive_sees_a_listening_port_and_rejects_junk(
    launcher: ModuleType,
) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert (
            launcher.tcp_alive("127.0.0.1:%d" % server.getsockname()[1])
            is True
        )
    assert launcher.tcp_alive("127.0.0.1:not-a-port") is False


def test_networked_reads_the_flatpak_context(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", True)

    def read(info: configparser.ConfigParser, path: object) -> list[str]:
        info.read_string("[Context]\nshared=network;ipc;\n")
        return [str(path)]

    monkeypatch.setattr(configparser.ConfigParser, "read", read)
    assert launcher.networked() is True

    def read_without(
        info: configparser.ConfigParser, path: object
    ) -> list[str]:
        info.read_string("[Context]\nshared=ipc;\n")
        return [str(path)]

    monkeypatch.setattr(configparser.ConfigParser, "read", read_without)
    assert launcher.networked() is False


def test_the_service_blocker_names_each_missing_piece(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", True)
    blocked = launcher.service_blocker()
    assert blocked is not None and "inside Flatpak" in blocked

    monkeypatch.setattr(launcher, "FLATPAK", False)
    home = tmp_path / "home"
    binary = home / ".local/bin/kpclientd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    blocked = launcher.service_blocker()
    assert blocked is not None and "client.toml is not at" in blocked

    config = home / ".local/katzenpost/client.toml"
    config.parent.mkdir(parents=True)
    config.write_text("x\n")
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)
    blocked = launcher.service_blocker()
    assert blocked is not None and "systemctl not found" in blocked


def test_install_service_mode_reports_success(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(launcher, "service_blocker", lambda: None)
    installs: list[bool] = []

    def install() -> bool:
        installs.append(True)
        return True

    monkeypatch.setattr(launcher, "install_service", install)
    monkeypatch.setattr(sys, "argv", ["launcher", "--install-service"])
    launcher.main()
    assert installs == [True]
    assert "installed, enabled, and started" in capsys.readouterr().out


def test_a_reachable_docker_daemon_launches_the_gui(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "networked", lambda: True)
    monkeypatch.setattr(launcher, "tcp_alive", lambda _address: True)
    called: list[tuple[object, ...]] = []

    def call(*args: object) -> int:
        called.append(args)
        return 0

    monkeypatch.setattr(launcher.subprocess, "call", call)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert called
    assert os.environ["KATZENQT_THINCLIENT_CONFIG"].endswith(
        "thinclient.toml"
    )


def test_an_installed_service_that_never_answers_hints_at_systemctl(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(launcher, "install_service", lambda: True)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="systemctl --user status kpclientd"):
        launcher.main()


def test_inside_flatpak_an_absent_daemon_hints_at_the_native_service(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", True)
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(
        SystemExit, match="install the native service and retry"
    ):
        launcher.main()


def test_inside_flatpak_the_tcp_launch_changes_into_the_app_dir(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "FLATPAK", True)
    monkeypatch.setattr(launcher, "networked", lambda: True)
    monkeypatch.setattr(launcher, "tcp_alive", lambda _address: True)
    monkeypatch.setattr(launcher.subprocess, "call", lambda *_a: 0)
    chdirs: list[str] = []
    monkeypatch.setattr(launcher.os, "chdir", appending_to(chdirs))
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert chdirs == ["/app/share/katzenqt"]
