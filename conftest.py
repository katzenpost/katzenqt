"""Root conftest for katzenqt tests.

Runs before pytest's `--doctest-modules` imports `src/katzenqt/persistent`, so
the SQLite URL frozen into `persistent._engine` at module load points at a
scratch file in a pytest-owned tmpdir, not the user's real
`~/.local/share/katzenqt/katzen.sqlite3`.
"""
import os
import tempfile


# Workers need separate databases; serial runs honor an explicit KQT_STATE.
if os.environ.get("PYTEST_XDIST_WORKER") or not os.environ.get("KQT_STATE"):
    _tmp_root = tempfile.mkdtemp(prefix="katzenqt-test-")
    os.environ["KQT_STATE"] = os.path.join(_tmp_root, f"test-{os.getpid()}")
