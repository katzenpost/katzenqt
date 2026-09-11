import configparser
import importlib.resources
import sys


def imports():
    import katzenqt  # noqa: F401
    import katzenpost_thinclient  # noqa: F401

    assert (importlib.resources.files("katzenqt") / "migrations").is_dir()


def permissions():
    info = configparser.ConfigParser()
    info.read("/.flatpak-info")
    shared = info.get("Context", "shared", fallback="").split(";")
    files = info.get("Context", "filesystems", fallback="").split(";")
    assert "network" not in shared
    assert [entry for entry in files if entry] == ["xdg-run/katzenpost:ro"]


def state(name):
    from katzenqt.persistent import state_file

    assert state_file.name == f"{name}.sqlite3"
    state_file.touch()


COMMANDS = {
    "imports": imports,
    "permissions": permissions,
    "state": state,
}


def main(argv):
    COMMANDS[argv[1]](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
