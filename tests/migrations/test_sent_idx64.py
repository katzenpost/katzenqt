"""The migration to ``sent_idx64`` carries each message of ours over to the
``Idx64`` of its box, where the Sent box still records the box."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import cbor2
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/katzenqt/migrations/versions/ddea2a53f288_conversationlog_sent_idx64.py"
)


def _record(seq: int) -> dict[str, object]:
    return {"seq": seq, "plaintext": None, "control": False, "written": 0}


def test_each_message_takes_its_boxs_idx64() -> None:
    spec = spec_from_file_location("sent_idx64", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE conversationlog (id INTEGER PRIMARY KEY, conversation_id INTEGER, sent_seq INTEGER)"
        )
        conn.exec_driver_sql("CREATE TABLE groupstate (conversation_id INTEGER PRIMARY KEY, state BLOB)")
        box = (2**64 - 2).to_bytes(8, "little") + bytes(96)
        body = {"sent": {"records": [[box, _record(4)]], "count": 5, "backfilled": -1}}
        conn.execute(
            sa.text("INSERT INTO groupstate VALUES (1, :state), (2, :broken)"),
            {"state": cbor2.dumps([2, body]), "broken": b"not cbor"},
        )
        conn.exec_driver_sql(
            "INSERT INTO conversationlog VALUES (1, 1, 4), (2, 1, 3), (3, 1, NULL), (4, 2, 4)"
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.exec_driver_sql("SELECT id, sent_idx64 FROM conversationlog ORDER BY id").all()
        assert rows == [(1, (2**64 - 2).to_bytes(8, "big")), (2, None), (3, None), (4, None)]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        columns = {c["name"] for c in sa.inspect(conn).get_columns("conversationlog")}
        assert columns == {"id", "conversation_id", "sent_seq"}
