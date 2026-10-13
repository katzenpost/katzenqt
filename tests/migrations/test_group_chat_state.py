"""The migration to group chat state adds its tables and columns, and the
messages already logged count from when it runs."""

import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/katzenqt/migrations/versions/3bee8c5a8252_group_chat_state.py"
)
TABLES = {"groupstate", "groupwritten", "grouprewrite"}


def test_upgrade_and_downgrade() -> None:
    spec = spec_from_file_location("group_chat_state", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE conversation (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE conversationlog (id INTEGER PRIMARY KEY, conversation_id INTEGER)"
        )
        conn.exec_driver_sql("INSERT INTO conversation VALUES (1)")
        conn.exec_driver_sql("INSERT INTO conversationlog VALUES (1, 1)")
        before = time.time()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert TABLES <= set(sa.inspect(conn).get_table_names())
        ((logged_at, sent_idx64),) = conn.exec_driver_sql(
            "SELECT logged_at, sent_idx64 FROM conversationlog"
        ).all()
        assert logged_at >= before and sent_idx64 is None
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert not TABLES & set(sa.inspect(conn).get_table_names())
        columns = {c["name"] for c in sa.inspect(conn).get_columns("conversationlog")}
        assert columns == {"id", "conversation_id"}
