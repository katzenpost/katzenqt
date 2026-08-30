import importlib.util
import stat
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    name = f"flatpak_launcher_{id(tmp_path)}"
    path = Path(__file__).parents[1] / "packaging" / "flatpak" / "launcher.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


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
