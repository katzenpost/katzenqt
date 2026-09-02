#!/usr/bin/python3
"""Resolve a kpclientd endpoint and launch the katzenqt GUI against it.

The Flatpak sandbox cannot open network sockets, so the GUI must reach a
kpclientd over a Unix socket instead of dialling the mixnet itself. This
shared entry point, used by both ``make run`` and the Flatpak, finds a
running daemon or starts one -- the native systemd user service via D-Bus
activation, a supervised bundled daemon, or a Docker testnet over TCP --
writes the matching thinclient config, then execs the GUI binary.
"""
import configparser
import fcntl
import importlib.resources
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = "network.katzenpost.katzenqt"
BUS = "network.katzenpost.kpclientd"
FLATPAK = Path("/.flatpak-info").exists()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
ROOT = RUNTIME / ("app" if FLATPAK else "") / APP
HOST = RUNTIME / "katzenpost" / "kpclientd.sock"
SOCKET = ROOT / "kpclientd.sock"
# Config templates and the systemd unit ship as package data, so they are
# found the same way regardless of where the module is installed from.
DATA = importlib.resources.files("katzenqt") / "data"
GUI = os.environ.get("KATZENQT_GUI", "/app/bin/katzenqt-bin")
DAEMON = os.environ.get(
    "KATZENQT_KPCLIENTD",
    Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/kpclientd"
    if FLATPAK
    else shutil.which("kpclientd") or "kpclientd",
)
_template = os.environ.get("KATZENQT_KPCLIENTD_CONFIG")
TEMPLATE = Path(_template) if _template else DATA / "client.toml"


def alive(path):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(0.2)
            client.connect(path)
        return True
    except OSError:
        return False


def networked():
    if not FLATPAK:
        return True
    info = configparser.ConfigParser()
    info.read("/.flatpak-info")
    return "network" in info.get("Context", "shared", fallback="").split(";")


def activatable():
    """Whether the kpclientd D-Bus service can autostart in this context.

    Inside Flatpak the host service is reached over the session bus.
    Outside it, only wait for D-Bus activation when the service file is
    actually installed, so a plain ``make run`` fails fast instead of
    polling for a daemon that will never appear.
    """
    if FLATPAK:
        return True
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local/share"
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    name = f"{BUS}.service"
    return any(
        (Path(base) / "dbus-1/services" / name).exists()
        for base in (data_home, *data_dirs.split(":"))
    )


def service_blocker():
    """Return why install_service() cannot proceed, or None if it can.

    The installed binary and its ``client.toml`` are preconditions
    (placed by ``make install-kpclient``); the reason is surfaced to the
    user so a failed ``make kpclientd.service`` is not a silent no-op.
    """
    if FLATPAK:
        return "cannot install a host service from inside Flatpak"
    if not (Path.home() / ".local/bin/kpclientd").is_file():
        return "kpclientd is not installed at ~/.local/bin (run: make install-kpclient)"
    if not (Path.home() / ".local/katzenpost/client.toml").is_file():
        return "client.toml is not at ~/.local/katzenpost (run: make install-kpclient)"
    if not shutil.which("systemctl"):
        return "systemctl not found (a systemd user session is required)"
    return None


def install_service():
    """Install and start the native kpclientd user systemd service.

    Single implementation shared by ``make kpclientd.service`` (via
    ``launcher.py --install-service``) and the non-Flatpak runtime
    fallback: copy the systemd unit and its D-Bus activation file from
    package data, drop any stale legacy units, then reload, enable, and
    start the user service. Preconditions are checked by
    ``service_blocker``; the daemon config is never written here, so a
    hand-tuned one is untouched. Return True on success, or False when a
    precondition is unmet so the caller can fall back to guidance.
    """
    if service_blocker():
        return False
    units = Path.home() / ".config/systemd/user"
    services = Path.home() / ".local/share/dbus-1/services"
    units.mkdir(mode=0o700, parents=True, exist_ok=True)
    services.mkdir(parents=True, exist_ok=True)
    legacy = "dbus-network.katzenpost.kpclientd.Native.service"
    (units / legacy).unlink(missing_ok=True)
    (services / "network.katzenpost.kpclientd.Native.service").unlink(
        missing_ok=True
    )
    print("    writing kpclientd.service and its D-Bus activation file")
    unit = units / "kpclientd.service"
    unit.write_bytes((DATA / "kpclientd.service").read_bytes())
    (services / f"{BUS}.service").write_bytes(
        (DATA / "network.katzenpost.kpclientd.service").read_bytes()
    )
    print("    systemctl --user daemon-reload")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("    systemctl --user reenable + restart kpclientd")
    subprocess.run(
        ["systemctl", "--user", "reenable", "kpclientd"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["systemctl", "--user", "restart", "kpclientd"], check=True)
    return True


def thin(address, network="Unix"):
    path = ROOT / "thinclient.toml"
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = f'[Dial]\n  [Dial.{network}]\n    Address = "{address}"\n'
    if network == "Tcp":
        data += '    Network = "tcp"\n'
    path.write_text(data)
    path.chmod(0o600)
    return path


def daemon_config():
    path = ROOT / "client.toml"
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if not FLATPAK or os.environ.get("KATZENQT_NO_PORTAL"):
        return
    from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusVariant

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
    from PySide6.QtDBus import QDBusConnection, QDBusMessage

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
            raise SystemExit(
                "Docker kpclientd requires Flatpak network access"
            )
        if mode == "--status":
            print("docker")
            return
        os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(tcp, "Tcp"))
        if FLATPAK:
            os.chdir("/app/share/katzenqt")
        raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))
    address = endpoint()
    if not address and activatable():
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
    if not address and not networked():
        override = (
            "flatpak override --user --share=network "
            "--filesystem=~/.local/bin/kpclientd:ro"
        )
        raise SystemExit(
            "kpclientd is unavailable; install the native service or "
            f"run: {override} {APP}"
        )
    installed = False
    if not address and not FLATPAK:
        installed = install_service()
        if installed:
            for _ in range(600):
                address = endpoint()
                if address:
                    break
                time.sleep(0.05)
    if not address:
        if FLATPAK:
            hint = f"install kpclientd at {DAEMON}"
        elif installed:
            hint = "check 'systemctl --user status kpclientd'"
        else:
            hint = "install kpclientd with 'make install-kpclient' and retry"
        raise SystemExit(f"kpclientd is unavailable; {hint}")
    os.environ["KATZENQT_THINCLIENT_CONFIG"] = str(thin(address))
    if FLATPAK:
        os.chdir("/app/share/katzenqt")
    raise SystemExit(subprocess.call([GUI, *sys.argv[1:]]))


if __name__ == "__main__":
    main()
