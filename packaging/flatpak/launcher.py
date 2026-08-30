#!/usr/bin/python3
import configparser
import fcntl
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusVariant

APP = "network.katzenpost.katzenqt"
BUS = "network.katzenpost.kpclientd"
RUNTIME = Path(os.environ["XDG_RUNTIME_DIR"])
ROOT = RUNTIME / ("app" if Path("/.flatpak-info").exists() else "") / APP
HOST = RUNTIME / "katzenpost" / "kpclientd.sock"
SOCKET = ROOT / "kpclientd.sock"
CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "katzenpost"
)
PREFIX = (
    Path("/app")
    if Path("/.flatpak-info").exists()
    else Path(__file__).resolve().parents[2]
)
GUI = os.environ.get("KATZENQT_GUI", "/app/bin/katzenqt-bin")
DAEMON = os.environ.get(
    "KATZENQT_KPCLIENTD",
    Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/kpclientd"
    if PREFIX == Path("/app")
    else shutil.which("kpclientd") or "kpclientd",
)
TEMPLATE = Path(
    os.environ.get(
        "KATZENQT_KPCLIENTD_CONFIG",
        PREFIX / "share/katzenqt/client.toml"
        if PREFIX == Path("/app")
        else PREFIX / "config/client.toml",
    )
)


def alive(path):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.2)
            client.connect(path)
        return True
    except OSError:
        return False


def networked():
    if not Path("/.flatpak-info").exists():
        return True
    info = configparser.ConfigParser()
    info.read("/.flatpak-info")
    return "network" in info.get("Context", "shared", fallback="").split(";")


def thin(address, network="Unix"):
    path = CONFIG / "thinclient.toml"
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = f'[Dial]\n  [Dial.{network}]\n    Address = "{address}"\n'
    if network == "Tcp":
        data += '    Network = "tcp"\n'
    path.write_text(data)
    path.chmod(0o600)
    return path


def daemon_config():
    path = CONFIG / "client.toml"
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = TEMPLATE.read_text().replace(
        'Address = "@katzenpost"', f'Address = "{SOCKET}"', 1
    )
    data = data.replace(
        '    Addresses = ["$XDG_RUNTIME_DIR/katzenpost/kpclientd.sock"]\n', ""
    )
    path.write_text(data)
    path.chmod(0o600)
    return path


def portal():
    if PREFIX != Path("/app") or os.environ.get("KATZENQT_NO_PORTAL"):
        return
    values = {
        "reason": QDBusVariant("Keep the Katzenpost connection available"),
        "autostart": QDBusVariant(True),
        "commandline": QDBusVariant(["katzenqt", "--daemon"]),
    }
    call = QDBusMessage.createMethodCall(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Background",
        "RequestBackground",
    )
    call.setArguments(["", values])
    QDBusConnection.sessionBus().call(call)


def activate():
    call = QDBusMessage.createMethodCall(
        BUS, "/", "org.freedesktop.DBus.Peer", "Ping"
    )
    QDBusConnection.sessionBus().send(call)


def endpoint():
    for path in (HOST, SOCKET):
        if path.exists() and alive(str(path)):
            return path


def acquire():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (ROOT / "kpclientd.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        lock.close()


def supervise():
    lock = acquire()
    if not lock:
        return
    stopping = False
    process = None

    def stop(*_):
        nonlocal stopping
        stopping = True
        if process:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failures = 0
    while not stopping:
        if alive(str(HOST)):
            return
        SOCKET.unlink(missing_ok=True)
        started = time.monotonic()
        process = subprocess.Popen([str(DAEMON), "--config", daemon_config()])
        code = process.wait()
        if stopping or code == 0:
            return
        failures = 0 if time.monotonic() - started >= 60 else failures + 1
        if failures >= 8:
            raise SystemExit(code)
        time.sleep(min(2 ** (failures - 1), 30))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    tcp = os.environ.get("KATZENQT_KPCLIENTD_TCP")
    if tcp:
        if not networked():
            raise SystemExit(
                "Docker kpclientd requires Flatpak network access"
            )
        if mode == "--status":
            print("docker")
            return
        os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(tcp, "Tcp"))
        if PREFIX == Path("/app"):
            os.chdir("/app/share/katzenqt")
        raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))
    address = endpoint()
    if not address:
        activate()
        for _ in range(600):
            address = endpoint()
            if address:
                break
            time.sleep(0.05)
    if mode == "--status":
        print(
            "native"
            if address == HOST
            else "bundled"
            if address
            else "unavailable"
        )
        return
    if mode == "--daemon":
        if not address and networked():
            supervise()
        return
    if not address and networked() and Path(DAEMON).is_file():
        portal()
        subprocess.Popen([sys.executable, __file__, "--daemon"])
        for _ in range(600):
            address = endpoint()
            if address:
                break
            time.sleep(0.05)
    if not address:
        override = (
            "flatpak override --user --share=network "
            "--filesystem=~/.local/bin/kpclientd:ro"
        )
        raise SystemExit(
            "kpclientd is unavailable; install the native service or run: "
            f"{override} {APP}"
        )
    os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(address))
    if PREFIX == Path("/app"):
        os.chdir("/app/share/katzenqt")
    raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))


if __name__ == "__main__":
    main()
