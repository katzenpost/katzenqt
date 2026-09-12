#!/usr/bin/python3
import configparser
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from typing import Literal

APP = "network.katzenpost.katzenqt"
DATA = files("katzenqt") / "data"
FLATPAK = Path("/.flatpak-info").exists()


def _runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg)
    if hasattr(os, "getuid"):
        return Path(f"/run/user/{os.getuid()}")
    # os.getuid doesn't exist on Windows; this launcher's Unix-socket paths
    # are meaningless there anyway, but importing the module must not crash.
    return Path(tempfile.gettempdir())


RUNTIME = _runtime_dir()
ROOT = RUNTIME / ("app" if FLATPAK else "") / APP
HOST = RUNTIME / "katzenpost" / "kpclientd.sock"
SOCKET = ROOT / "kpclientd.sock"
ABSTRACT_SOCKET = "@katzenpost"
GUI = os.environ.get("KATZENQT_GUI") or (
    "/app/bin/katzenqt-bin" if FLATPAK else (shutil.which("katzenqt") or "katzenqt")
)


def alive(path: str) -> bool:
    """Return whether a Unix socket accepts a connection."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.2)
            client.connect("\0" + path[1:] if path.startswith("@") else path)
        return True
    except OSError:
        return False


def tcp_alive(address: str) -> bool:
    """Return whether a host:port address accepts a TCP connection."""
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except (OSError, ValueError):
        return False


def thin(address: str | Path, network: Literal["Unix", "Tcp"] = "Unix") -> Path:
    """Write a private thin-client configuration for the selected endpoint."""
    if network not in ("Unix", "Tcp"):
        raise ValueError("network must be Unix or Tcp")
    path = ROOT / "thinclient.toml"
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = f"[Dial]\n  [Dial.{network}]\n    Address = {json.dumps(str(address))}\n"
    if network == "Tcp":
        data += '    Network = "tcp"\n'
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)
    return path


def endpoint() -> Path | str | None:
    """Find a running filesystem or native abstract Unix socket."""
    for path in (HOST, SOCKET):
        if path.exists() and alive(str(path)):
            return path
    return ABSTRACT_SOCKET if alive(ABSTRACT_SOCKET) else None


def networked() -> bool:
    if not FLATPAK:
        return True
    info = configparser.ConfigParser()
    info.read("/.flatpak-info")
    return "network" in info.get("Context", "shared", fallback="").split(";")


def service_blocker() -> str | None:
    if FLATPAK:
        return "cannot install a host service from inside Flatpak"
    if not (Path.home() / ".local/bin/kpclientd").is_file():
        return "kpclientd is not installed at ~/.local/bin (run: make install-kpclient)"
    if not (Path.home() / ".local/katzenpost/client.toml").is_file():
        return "client.toml is not at ~/.local/katzenpost (run: make install-kpclient)"
    if not shutil.which("systemctl"):
        return "systemctl not found (a systemd user session is required)"
    return None


def install_service() -> bool:
    if service_blocker():
        return False
    units = Path.home() / ".config/systemd/user"
    units.mkdir(mode=0o700, parents=True, exist_ok=True)
    unit = units / "kpclientd.service"
    wanted = (DATA / "kpclientd.service").read_bytes()
    if not unit.exists() or unit.read_bytes() != wanted:
        unit.write_bytes(wanted)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "kpclientd"], check=True
    )
    return True


def main() -> None:

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--install-service":
        blocker = service_blocker()
        if blocker:
            raise SystemExit(f"kpclientd.service not installed: {blocker}")
        install_service()
        print("kpclientd.service installed, enabled, and started")
        return
    tcp = os.environ.get("KATZENQT_KPCLIENTD_TCP")
    if tcp:
        if not networked():
            raise SystemExit("Docker kpclientd requires Flatpak network access")
        reachable = tcp_alive(tcp)
        if mode == "--status":
            print("docker" if reachable else "unavailable")
            return
        if not reachable:
            raise SystemExit(f"kpclientd is unavailable; nothing is listening at {tcp}")
        os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(tcp, "Tcp"))
        if FLATPAK:
            os.chdir("/app/share/katzenqt")
        raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))
    address = endpoint()
    if mode == "--status":
        print(
            "native"
            if address in (HOST, ABSTRACT_SOCKET)
            else "bundled"
            if address
            else "unavailable"
        )
        return
    installed = False
    auto_install = not os.environ.get("KATZENQT_NO_AUTO_INSTALL")
    if not address and not FLATPAK and auto_install:
        # A plain launch with no daemon reachable installs and starts a
        # persistent host service -- a real, opt-out-able side effect, not
        # just a read-only failure. Set KATZENQT_NO_AUTO_INSTALL=1 to get
        # the old read-only behavior (a clear error) instead.
        installed = install_service()
        if installed:
            for _ in range(600):
                address = endpoint()
                if address:
                    break
                time.sleep(0.05)
    if not address:
        if FLATPAK:
            hint = "install the native service and retry"
        elif installed:
            hint = "check 'systemctl --user status kpclientd'"
        elif not auto_install:
            hint = "install kpclientd with 'make kpclientd.service' and retry"
        else:
            hint = "install kpclientd with 'make install-kpclient' and retry"
        raise SystemExit(f"kpclientd is unavailable; {hint}")
    os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(address))
    if FLATPAK:
        os.chdir("/app/share/katzenqt")
    raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))


if __name__ == "__main__":
    main()
