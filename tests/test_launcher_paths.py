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
