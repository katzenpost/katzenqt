from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "src/katzenqt/migrations/versions"


def _migration(name: str) -> ModuleType:
    spec = spec_from_file_location(name, VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_cursors_survive_transfer_state_migrations() -> None:
    missing = _migration("9e62c30a8b14_substream_failure.py")
    pause = _migration("31a0f3b426c8_pause.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE readcapwal (id TEXT PRIMARY KEY, next_index BLOB)"
        )
        conn.exec_driver_sql(
            "INSERT INTO readcapwal VALUES (?, ?)", ("stream", b"i" * 104),
        )
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            missing.upgrade()
            pause.upgrade()
        row = conn.exec_driver_sql(
            "SELECT next_index, paused, substream_missing_since, "
            "substream_failure FROM readcapwal"
        ).one()
        assert row == (b"i" * 104, 0, None, None)
        conn.exec_driver_sql("UPDATE readcapwal SET paused = 1")
        with Operations.context(context):
            pause.downgrade()
            missing.downgrade()
        assert conn.exec_driver_sql(
            "SELECT next_index FROM readcapwal"
        ).scalar_one() == b"i" * 104
    engine.dispose()
