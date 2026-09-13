import importlib.util

import pytest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CHECK = ROOT / "packaging" / "flatpak" / "check.py"


def _load():
    spec = importlib.util.spec_from_file_location("flatpak_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_commands_are_dispatchable():
    module = _load()
    assert set(module.COMMANDS) == {"imports", "permissions", "state"}


def test_imports_check_finds_migrations():
    _load().imports()


@pytest.mark.parametrize("extra", ["", "xdg-config/kdeglobals:ro;"])
def test_permissions_allow_only_the_runtime_socket(monkeypatch, extra):
    module = _load()

    def read(info, path):
        info.read_string(
            "[Context]\nshared=ipc;\nfilesystems=xdg-run/katzenpost:ro;" + extra
        )
        return [path]

    monkeypatch.setattr(module.configparser.ConfigParser, "read", read)
    module.permissions()


@pytest.mark.parametrize(
    "context",
    [
        "shared=network;\nfilesystems=xdg-run/katzenpost:ro;",
        "filesystems=xdg-run/katzenpost;",
        "filesystems=xdg-run/katzenpost:ro;home;",
        "filesystems=xdg-config/kdeglobals:ro;",
    ],
)
def test_permissions_reject_broad_or_missing_access(monkeypatch, context):
    module = _load()

    def read(info, path):
        info.read_string("[Context]\n" + context)
        return [path]

    monkeypatch.setattr(module.configparser.ConfigParser, "read", read)
    with pytest.raises(RuntimeError, match="unexpected Flatpak permissions"):
        module.permissions()
