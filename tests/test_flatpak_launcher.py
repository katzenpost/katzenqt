import importlib
import stat
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    sys.modules.pop("katzenqt.launcher", None)
    module = importlib.import_module("katzenqt.launcher")
    yield module
    sys.modules.pop("katzenqt.launcher", None)


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


def test_status_reports_all_endpoints(launcher, monkeypatch, capsys):
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


def test_unavailable_daemon_never_starts_gui(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "endpoint", lambda: None)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *_: pytest.fail("GUI started")
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit, match="kpclientd is unavailable"):
        launcher.main()


def test_running_daemon_launches_gui(launcher, monkeypatch):
    launched = []
    monkeypatch.setattr(launcher, "endpoint", lambda: launcher.HOST)
    monkeypatch.setattr(
        launcher.subprocess, "call", lambda *a: launched.append(a) or 0
    )
    monkeypatch.setattr(sys, "argv", ["launcher"])
    with pytest.raises(SystemExit):
        launcher.main()
    assert launched


def test_generated_configs_live_in_runtime_dir(launcher, tmp_path):
    thin = launcher.thin("/run/test.sock")
    assert launcher.ROOT in thin.parents
    assert str(tmp_path / "config") not in str(thin)


def test_endpoint_reaches_default_abstract_socket(launcher, monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _: False)
    monkeypatch.setattr(launcher, "alive", lambda address: address == "@katzenpost")
    assert launcher.endpoint() == "@katzenpost"
