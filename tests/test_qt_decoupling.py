"""Pin: ``katzenqt`` and the headless submodules must not load PySide6.

Headless consumers (pytest collection, the integration runner, anyone
who just needs ``persistent`` / ``models`` / ``network``) used to pay an
implicit Qt cost because ``src/katzenqt/__init__.py`` was a one-liner
``from .katzen import cli``, and ``katzen.py`` pulls in PySide6 at
module top level. That's now lazy via PEP 562 ``__getattr__`` — this
test guards the lazy-import contract.

Each test runs the import in a fresh subprocess so previous test
collection doesn't pollute ``sys.modules``.
"""
import subprocess
import sys


_PYTHON = sys.executable


def _imports_pyside6(snippet: str) -> bool:
    """Run snippet in a fresh interpreter; return True iff PySide6 ended up loaded."""
    code = (
        snippet
        + "\nimport sys\n"
          "loaded = sorted(m for m in sys.modules if m.startswith('PySide6'))\n"
          "print('PYSIDE_LOADED', bool(loaded), loaded)\n"
    )
    out = subprocess.run(
        [_PYTHON, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if line.startswith("PYSIDE_LOADED"):
            return line.split()[1] == "True"
    raise AssertionError(f"sentinel not found in subprocess stdout:\n{out}")


def test_plain_import_katzenqt_does_not_load_pyside6():
    assert not _imports_pyside6("import katzenqt")


def test_import_katzenqt_persistent_does_not_load_pyside6():
    assert not _imports_pyside6("import katzenqt.persistent")


def test_import_katzenqt_network_does_not_load_pyside6():
    assert not _imports_pyside6("import katzenqt.network")


def test_import_katzenqt_models_does_not_load_pyside6():
    # ConversationUIState used to live here and dragged in Qt via its
    # ConversationLogModel/QStandardItem/QQmlPropertyMap fields. It now
    # lives in katzenqt.qt_models so this module can stay headless.
    assert not _imports_pyside6("import katzenqt.models")


def test_integration_runner_imports_do_not_load_pyside6():
    # Regression guard for the precise import set the docker-integration
    # subprocess pulls in. If a future contributor adds a top-level
    # PySide6-touching import to integration_runner (or to one of its
    # transitive deps) the docker-integration CI will break with
    # `ImportError: libEGL.so.1` and this test will catch it locally
    # first.
    assert not _imports_pyside6(
        "from katzenqt import models, network, persistent\n"
        "from katzenqt import integration_runner\n"
    )


def test_cli_attribute_still_resolves_and_loads_pyside6():
    # The console script `katzenqt = katzenqt:cli` resolves cli via
    # `getattr(katzenqt_module, 'cli')`. That MUST still work AND it
    # MUST drag PySide6 in (the GUI lives there).
    assert _imports_pyside6(
        "from katzenqt import cli\n"
        "assert callable(cli)\n"
    )
