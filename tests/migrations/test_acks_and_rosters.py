"""The migration that adds what acknowledgements and rosters need must
leave every existing read cap and peer as it found it."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "src/katzenqt/migrations/versions"
NEW_TABLES = {"acklevel", "introductionseen", "rostermember", "sentbox"}


def _migration(name: str) -> ModuleType:
    spec = spec_from_file_location(name, VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_read_caps_and_peers_survive() -> None:
    migration = _migration("d92a4cc162b9_acks_rosters_and_sent_boxes.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE conversation (id INTEGER PRIMARY KEY)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE readcapwal (id TEXT PRIMARY KEY, next_index BLOB)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE conversationpeer "
            "(id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN)"
        )
        conn.exec_driver_sql(
            "INSERT INTO readcapwal VALUES (?, ?)", ("stream", b"i" * 104)
        )
        conn.exec_driver_sql(
            "INSERT INTO conversationpeer VALUES (1, 'bob', 1)"
        )
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            migration.upgrade()
        assert NEW_TABLES <= set(sa.inspect(conn).get_table_names())
        assert conn.exec_driver_sql(
            "SELECT next_index, frontier_index, last_read_index, acked_index "
            "FROM readcapwal"
        ).one() == (b"i" * 104, None, None, None)
        assert conn.exec_driver_sql(
            "SELECT name, active, acked_position FROM conversationpeer"
        ).one() == ("bob", 1, None)

        conn.exec_driver_sql(
            "UPDATE readcapwal SET last_read_index = ?", (b"r" * 104,)
        )
        with Operations.context(context):
            migration.downgrade()
        assert not NEW_TABLES & set(sa.inspect(conn).get_table_names())
        assert (
            conn.exec_driver_sql(
                "SELECT next_index FROM readcapwal"
            ).scalar_one()
            == b"i" * 104
        )
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM conversationpeer"
            ).scalar_one()
            == "bob"
        )
    engine.dispose()
