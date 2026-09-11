import importlib.util
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
