#!/usr/bin/python3
import configparser
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Literal

APP = "network.katzenpost.katzenqt"
FLATPAK = Path("/.flatpak-info").exists()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
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


def main() -> None:
    """Report daemon availability or launch the GUI with its socket."""

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    tcp = os.environ.get("KATZENQT_KPCLIENTD_TCP")
    if tcp:
        if not networked():
            raise SystemExit("Docker kpclientd requires Flatpak network access")
        if mode == "--status":
            print("docker")
            return
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
    if not address:
        raise SystemExit(
            "kpclientd is unavailable; start it and retry"
        )
    os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(address))
    if FLATPAK:
        os.chdir("/app/share/katzenqt")
    raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))


if __name__ == "__main__":
    main()
