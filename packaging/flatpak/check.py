import configparser
import importlib
import importlib.metadata
import importlib.resources
import re
import sys


def _pinned_thinclient() -> str:
    """The thin-client version katzenqt itself declares.

    Read from katzenqt's own metadata rather than written here, so a
    version bump does not have to remember this file. The hardcoded
    0.0.24 it replaces outlived the pin and failed the build.
    """
    for req in importlib.metadata.requires("katzenqt") or ():
        pin = re.match(r"katzenpost-thinclient==([0-9][0-9.]*)", req)
        if pin:
            return pin.group(1)
    raise RuntimeError("katzenqt declares no thin-client pin")


def imports() -> None:
    for name in ("katzenqt", "katzenpost_thinclient", "rustic_audio_tool"):
        importlib.import_module(name)
    installed = importlib.metadata.version("katzenpost-thinclient")
    if installed != _pinned_thinclient():
        raise RuntimeError("unexpected thin-client version")
    package = importlib.resources.files("katzenqt")
    if not (package / "migrations").is_dir():
        raise RuntimeError("packaged migrations are missing")
    if not (package / "data" / "alembic.ini").is_file():
        raise RuntimeError("packaged migration configuration is missing")


def permissions() -> None:
    info = configparser.ConfigParser()
    if not info.read("/.flatpak-info"):
        raise RuntimeError("Flatpak metadata is missing")
    shared = set(info.get("Context", "shared", fallback="").split(";"))
    files = set(info.get("Context", "filesystems", fallback="").split(";"))
    files.discard("")
    required = {"xdg-run/katzenpost:ro"}
    allowed = required | {"xdg-config/kdeglobals:ro"}
    if "network" in shared or not required <= files <= allowed:
        raise RuntimeError("unexpected Flatpak permissions")


def state(name: str) -> None:
    from katzenqt.persistent import state_file

    if state_file.name != f"{name}.sqlite3":
        raise RuntimeError("unexpected state path")
    state_file.touch()


COMMANDS = {
    "imports": imports,
    "permissions": permissions,
    "state": state,
}


def main(argv: list[str]) -> None:
    COMMANDS[argv[1]](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
