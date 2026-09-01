import importlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    # Re-import fresh so module-level constants (RUNTIME, ROOT, FLATPAK) are
    # recomputed against the patched environment.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sys.modules.pop("katzenqt.launcher", None)
    module = importlib.import_module("katzenqt.launcher")
    yield module
    sys.modules.pop("katzenqt.launcher", None)


def _seed_daemon(home, config_text):
    """Place the binary + client.toml preconditions install_service expects."""
    binary = home / ".local/bin/kpclientd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    config = home / ".local/katzenpost/client.toml"
    config.parent.mkdir(parents=True)
    config.write_text(config_text)


def test_thin_configs(launcher):
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


def test_daemon_config_has_one_private_listener(
    launcher, tmp_path, monkeypatch
):
    template = tmp_path / "client.toml"
    template.write_text(
        '[Listen]\nAddress = "@katzenpost"\n'
        '    Addresses = ["$XDG_RUNTIME_DIR/katzenpost/'
        'kpclientd.sock"]\n'
    )
    monkeypatch.setattr(launcher, "TEMPLATE", template)
    result = launcher.daemon_config()
    data = result.read_text()
    assert f'Address = "{launcher.SOCKET}"' in data
    assert "Addresses" not in data
    assert stat.S_IMODE(result.stat().st_mode) == 0o600


def test_endpoint_prefers_host(launcher, monkeypatch):
    monkeypatch.setattr(
        Path, "exists", lambda path: path in (launcher.HOST, launcher.SOCKET)
    )
    monkeypatch.setattr(launcher, "alive", lambda path: True)
    assert launcher.endpoint() == launcher.HOST


def test_endpoint_uses_private_fallback(launcher, monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda path: path == launcher.SOCKET)
    monkeypatch.setattr(launcher, "alive", lambda path: True)
    assert launcher.endpoint() == launcher.SOCKET


def test_acquire_is_exclusive(launcher):
    first = launcher.acquire()
    assert first is not None
    assert launcher.acquire() is None
    first.close()
    second = launcher.acquire()
    assert second is not None
    second.close()


def test_status_reports_all_endpoints(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "activate", lambda: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
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


def test_tcp_requires_network_permission(launcher, monkeypatch):
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "networked", lambda: False)
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="requires Flatpak network access"):
        launcher.main()


def test_secure_mode_never_starts_daemon(launcher, monkeypatch):
    started = []
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(launcher, "activate", lambda: None)
    monkeypatch.setattr(launcher, "networked", lambda: False)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: started.append(args),
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="kpclientd is unavailable"):
        launcher.main()
    assert started == []


def test_tcp_status_does_not_start_gui(launcher, monkeypatch, capsys):
    monkeypatch.setenv("KATZENQT_KPCLIENTD_TCP", "127.0.0.1:64331")
    monkeypatch.setattr(launcher, "networked", lambda: True)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *_: pytest.fail("GUI started")
    )
    monkeypatch.setattr(sys, "argv", ["launcher", "--status"])
    launcher.main()
    assert capsys.readouterr().out.strip() == "docker"


def test_dev_mode_installs_service_then_launches(launcher, monkeypatch):
    calls = {"install": 0}

    def fake_install():
        calls["install"] += 1
        return True

    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "activatable", lambda: False)
    monkeypatch.setattr(launcher, "networked", lambda: True)
    monkeypatch.setattr(launcher, "DAEMON", "/nonexistent/kpclientd")
    monkeypatch.setattr(launcher, "install_service", fake_install)
    # No socket until the service has been installed and started.
    monkeypatch.setattr(
        launcher, "endpoint", lambda: launcher.HOST if calls["install"] else None
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launched = []
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *a: launched.append(a) or 0
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert calls["install"] == 1
    assert launched


def test_dev_mode_reports_when_service_cannot_install(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(launcher, "activatable", lambda: False)
    monkeypatch.setattr(launcher, "networked", lambda: True)
    monkeypatch.setattr(launcher, "DAEMON", "/nonexistent/kpclientd")
    monkeypatch.setattr(launcher, "install_service", lambda: False)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *_: pytest.fail("GUI started")
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="make install-kpclient"):
        launcher.main()


def test_install_service_writes_units_and_starts(
    launcher, monkeypatch, tmp_path
):
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_daemon(tmp_path, "hand-tuned\n")
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: "/usr/bin/systemctl"
    )
    ran = []
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *a, **k: ran.append(a[0]) or subprocess.CompletedProcess(a, 0),
    )
    assert launcher.install_service() is True
    assert (tmp_path / ".config/systemd/user/kpclientd.service").is_file()
    assert (
        tmp_path / ".local/share/dbus-1/services" / f"{launcher.BUS}.service"
    ).is_file()
    # The daemon config is a precondition, never written or overwritten here.
    assert (
        tmp_path / ".local/katzenpost/client.toml"
    ).read_text() == "hand-tuned\n"
    assert ["systemctl", "--user", "daemon-reload"] in ran
    assert ["systemctl", "--user", "restart", "kpclientd"] in ran


def test_install_service_skips_without_binary(launcher, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    called = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: called.append(a)
    )
    assert launcher.install_service() is False
    assert called == []


def test_install_service_skips_without_config(launcher, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = tmp_path / ".local/bin/kpclientd"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")  # binary present, but no client.toml
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: "/usr/bin/systemctl"
    )
    called = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: called.append(a)
    )
    assert launcher.install_service() is False
    assert called == []


def test_install_service_mode_maps_result_to_exit_code(launcher, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["launcher", "--install-service"])
    monkeypatch.setattr(launcher, "install_service", lambda: True)
    with pytest.raises(SystemExit) as ok:
        launcher.main()
    assert ok.value.code == 0
    monkeypatch.setattr(launcher, "install_service", lambda: False)
    with pytest.raises(SystemExit) as fail:
        launcher.main()
    assert fail.value.code == 1


def test_activatable_requires_installed_service(
    launcher, monkeypatch, tmp_path
):
    monkeypatch.setattr(launcher, "FLATPAK", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "sys"))
    assert launcher.activatable() is False
    service = (
        tmp_path / "data" / "dbus-1" / "services" / f"{launcher.BUS}.service"
    )
    service.parent.mkdir(parents=True)
    service.write_text("[D-BUS Service]\n")
    assert launcher.activatable() is True


def test_generated_configs_live_in_runtime_dir(launcher, tmp_path):
    thin = launcher.thin("/run/test.sock")
    assert launcher.ROOT in thin.parents
    assert str(tmp_path / "config") not in str(thin)
