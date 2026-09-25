from __future__ import annotations

import importlib
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[ModuleType]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sys.modules.pop("katzenqt.launcher", None)
    module = importlib.import_module("katzenqt.launcher")
    yield module
    sys.modules.pop("katzenqt.launcher", None)


def test_thin_configs(launcher: ModuleType) -> None:
    unix = launcher.thin("/run/test.sock")
    assert (
        unix.read_text()
        == '[Dial]\n  [Dial.Unix]\n    Address = "/run/test.sock"\n'
    )
    assert stat.S_IMODE(unix.stat().st_mode) == 0o600
    tcp = launcher.thin("127.0.0.1:64331", "Tcp")
    assert (
        tcp.read_text()
        == '[Dial]\n  [Dial.Tcp]\n    Address = "127.0.0.1:64331"\n'
        '    Network = "tcp"\n'
    )


def test_endpoint_prefers_host(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Path, "exists", lambda path: path in (launcher.HOST, launcher.SOCKET)
    )
    monkeypatch.setattr(launcher, "alive", lambda path: True)
    assert launcher.endpoint() == launcher.HOST


def test_endpoint_uses_private_fallback(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda path: path == launcher.SOCKET)
    monkeypatch.setattr(launcher, "alive", lambda path: True)
    assert launcher.endpoint() == launcher.SOCKET


def test_status_reports_all_endpoints(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for endpoint, expected in (
        (launcher.HOST, "native"),
        (launcher.SOCKET, "bundled"),
        (None, "unavailable"),
    ):
        monkeypatch.setattr(
            launcher, "endpoint", lambda endpoint=endpoint: endpoint
        )
        monkeypatch.setattr(sys, "argv", ["launcher", "--status"])
        launcher.main()
        assert capsys.readouterr().out.strip() == expected


def test_unavailable_daemon_never_starts_gui(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(launcher, "install_service", lambda: False)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *_: pytest.fail("GUI started")
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="kpclientd is unavailable"):
        launcher.main()


def test_running_daemon_launches_gui(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[tuple[object, ...]] = []

    def call(*args: object) -> int:
        launched.append(args)
        return 0

    monkeypatch.setattr(launcher, "endpoint", lambda: launcher.HOST)
    monkeypatch.setattr(launcher.subprocess, "call", call)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert launched


def test_generated_configs_live_in_runtime_dir(
    launcher: ModuleType, tmp_path: Path,
) -> None:
    thin = launcher.thin("/run/test.sock")
    assert launcher.ROOT in thin.parents
    assert str(tmp_path / "config") not in str(thin)


def test_endpoint_reaches_default_abstract_socket(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _: False)
    monkeypatch.setattr(
        launcher, "alive", lambda address: address == "@katzenpost"
    )
    assert launcher.endpoint() == "@katzenpost"


def _recording_run(
    ran: "list[object]",
) -> "Callable[..., subprocess.CompletedProcess[str]]":
    def run(*a: object, **k: object) -> "subprocess.CompletedProcess[str]":
        ran.append(a[0])
        return subprocess.CompletedProcess(a, 0)  # type: ignore[arg-type]

    return run


def _counting_install(calls: "dict[str, int]") -> "Callable[[], bool]":
    def install() -> bool:
        calls["install"] += 1
        return True

    return install


def _seed_daemon(home: Path, config_text: str) -> None:
    binary = home / ".local/bin/kpclientd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    config = home / ".local/katzenpost/client.toml"
    config.parent.mkdir(parents=True)
    config.write_text(config_text)


def test_networked_true_outside_flatpak(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    assert launcher.networked() is True


def test_tcp_status_reports_docker_when_reachable(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "tcp_alive", lambda address: True)
    monkeypatch.setattr(sys, "argv", ["launcher", "--status"])
    launcher.main()
    assert capsys.readouterr().out.strip() == "docker"


def test_tcp_status_reports_unavailable_when_unreachable(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "tcp_alive", lambda address: False)
    monkeypatch.setattr(sys, "argv", ["launcher", "--status"])
    launcher.main()
    assert capsys.readouterr().out.strip() == "unavailable"


def test_tcp_launch_fails_fast_when_unreachable(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "tcp_alive", lambda address: False)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *_: pytest.fail("GUI started")
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="kpclientd is unavailable"):
        launcher.main()


def test_tcp_requires_network_in_flatpak(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "FLATPAK", True)
    monkeypatch.setattr(launcher, "networked", lambda: False)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="network access"):
        launcher.main()


def test_install_service_writes_plain_unit_and_starts(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_daemon(tmp_path, "hand-tuned\n")
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: "/usr/bin/systemctl",
    )
    ran: list[object] = []
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        _recording_run(ran),
    )
    assert launcher.install_service() is True
    assert (tmp_path / ".config/systemd/user/kpclientd.service").is_file()
    config = tmp_path / ".local/katzenpost/client.toml"
    assert config.read_text() == "hand-tuned\n"
    assert ["systemctl", "--user", "daemon-reload"] in ran
    assert ["systemctl", "--user", "enable", "--now", "kpclientd"] in ran


def test_install_service_keeps_a_matching_unit(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_daemon(tmp_path, "hand-tuned\n")
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: "/usr/bin/systemctl",
    )
    unit = tmp_path / ".config/systemd/user/kpclientd.service"
    unit.parent.mkdir(parents=True)
    unit.write_bytes((launcher.DATA / "kpclientd.service").read_bytes())
    ran: list[object] = []
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        _recording_run(ran),
    )
    assert launcher.install_service() is True
    assert ["systemctl", "--user", "daemon-reload"] not in ran
    assert ["systemctl", "--user", "enable", "--now", "kpclientd"] in ran


def test_install_service_skips_without_binary(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    called = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: called.append(a),
    )
    assert launcher.install_service() is False
    assert called == []


def test_install_service_mode_reports_blocker(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["launcher", "--install-service"])
    monkeypatch.setattr(
        launcher, "service_blocker", lambda: "run: make install-kpclient",
    )
    monkeypatch.setattr(
        launcher, "install_service", lambda: pytest.fail("should not install")
    )
    with pytest.raises(SystemExit, match="make install-kpclient"):
        launcher.main()


def test_dev_mode_installs_service_then_launches(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"install": 0}
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(
        launcher,
        "install_service",
        _counting_install(calls),
    )
    monkeypatch.setattr(
        launcher,
        "endpoint",
        lambda: launcher.HOST if calls["install"] else None,
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launched: list[tuple[object, ...]] = []

    def record_call(*a: object) -> int:
        launched.append(a)
        return 0

    monkeypatch.setattr(launcher.subprocess, "call", record_call)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert calls["install"] == 1
    assert launched


def test_no_auto_install_env_var_opts_out(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_NO_AUTO_INSTALL", "1")
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(
        launcher, "install_service", lambda: pytest.fail("should not install")
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="kpclientd is unavailable"):
        launcher.main()
