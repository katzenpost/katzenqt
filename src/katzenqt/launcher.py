#!/usr/bin/python3
import importlib.resources
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

APP = "network.katzenpost.katzenqt"
FLATPAK = Path("/.flatpak-info").exists()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
ROOT = RUNTIME / ("app" if FLATPAK else "") / APP
HOST = RUNTIME / "katzenpost" / "kpclientd.sock"
SOCKET = ROOT / "kpclientd.sock"
DATA = importlib.resources.files("katzenqt") / "data"
GUI = os.environ.get("KATZENQT_GUI") or (
    "/app/bin/katzenqt-bin" if FLATPAK else (shutil.which("katzenqt") or "katzenqt")
)


def alive(path):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.2)
            client.connect(path)
        return True
    except OSError:
        return False


def thin(address, network="Unix"):
    path = ROOT / "thinclient.toml"
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = f'[Dial]\n  [Dial.{network}]\n    Address = "{address}"\n'
    if network == "Tcp":
        data += '    Network = "tcp"\n'
    path.write_text(data)
    path.chmod(0o600)
    return path


def endpoint():
    for path in (HOST, SOCKET):
        if path.exists() and alive(str(path)):
            return path


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    address = endpoint()
    if mode == "--status":
        print(
            "native"
            if address == HOST
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
