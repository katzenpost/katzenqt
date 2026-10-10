"""The migration to group state works out every roster kept by the old
rules and writes it as an explicit one."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import cbor2
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from katzenqt.groupchat import state

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "src/katzenqt/migrations/versions"
OLD_TABLES = {"acklevel", "introductionseen", "rostermember", "sentbox", "outgoingacks"}

ALICE, BOB, CAROL, DAVE = (bytes([n]) * 32 for n in (1, 2, 3, 4))


def _migration(name: str) -> ModuleType:
    spec = spec_from_file_location(name, VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _at(position: int) -> bytes:
    return position.to_bytes(8, "big")


def _index(position: int) -> bytes:
    return position.to_bytes(8, "little") + bytes(96)


def _old_schema(conn: sa.Connection, context: MigrationContext) -> None:
    conn.exec_driver_sql(
        "CREATE TABLE conversation (id INTEGER PRIMARY KEY, own_peer_id INTEGER, write_cap TEXT)"
    )
    conn.exec_driver_sql("CREATE TABLE writecapwal (id TEXT PRIMARY KEY, write_cap BLOB)")
    conn.exec_driver_sql(
        "CREATE TABLE readcapwal (id TEXT PRIMARY KEY, read_cap BLOB, next_index BLOB)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE conversationpeer "
        "(id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN, read_cap_id TEXT)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE conversationpeerlink (conversation_peer_id INTEGER, conversation_id INTEGER)"
    )
    with Operations.context(context):
        _migration("d92a4cc162b9_acks_rosters_and_sent_boxes.py").upgrade()
        _migration("607a20224c70_outgoing_acks.py").upgrade()


def _alices_group(conn: sa.Connection) -> None:
    """Alice founded the group and inducted bob; bob inducted carol, and
    alice numbered carol; carol introduced dave, whom alice has not
    numbered. Two other conversations: one without rosters, one whose write
    cap is not provisioned yet."""
    sql = conn.exec_driver_sql
    sql("INSERT INTO writecapwal VALUES ('w', ?)", (bytes(32) + ALICE + _index(100),))
    sql("INSERT INTO conversation VALUES (1, 10, 'w')")
    sql("INSERT INTO writecapwal VALUES ('w2', ?)", (bytes(32) + ALICE + _index(500),))
    sql("INSERT INTO conversation VALUES (2, 20, 'w2')")
    sql("INSERT INTO conversation VALUES (3, 30, NULL)")
    for peer, name, key, rcw, start in (
        (10, "alice", ALICE, "ra", 100),
        (11, "bob", BOB, "rb", 200),
        (12, "carol", CAROL, "rc", 300),
        (13, ":substream:x", DAVE, "rs", 1),
        (14, "gone", DAVE, "rg", 1),
        (15, "unread", DAVE, "ru", None),
    ):
        sql(
            "INSERT INTO readcapwal (id, read_cap, next_index) VALUES (?, ?, ?)",
            (rcw, key + _index(start or 0), None if start is None else _index(start + 9)),
        )
        sql(
            "INSERT INTO conversationpeer (id, name, active, read_cap_id) VALUES (?, ?, ?, ?)",
            (peer, name, peer != 14, rcw),
        )
        sql("INSERT INTO conversationpeerlink VALUES (?, 1)", (peer,))
    sql("UPDATE readcapwal SET last_read_index = ?, acked_index = ? WHERE id = 'rb'",
        (_index(208), _index(207)))
    sql("UPDATE conversationpeer SET acked_position = ? WHERE id = 11", (_at(103),))
    for key, seen, base_roster, introducer, position in (
        (ALICE, 106, cbor2.dumps([ALICE]), None, None),
        (BOB, 208, None, ALICE, 105),
        (CAROL, 305, None, BOB, 207),
    ):
        sql(
            "INSERT INTO rostermember VALUES (1, ?, ?, ?, ?, ?)",
            (key, _at(seen), base_roster, introducer, None if position is None else _at(position)),
        )
    for introducer, position, member in ((ALICE, 105, BOB), (BOB, 207, CAROL), (CAROL, 303, DAVE)):
        sql(
            "INSERT INTO introductionseen (id, conversation_id, introducer_key, position, member_key)"
            " VALUES (?, 1, ?, ?, ?)",
            (f"i{position}", introducer, _at(position), member),
        )
    sql("INSERT INTO acklevel VALUES (1, ?, ?, 1, ?, NULL)", (ALICE, _at(106), _at(207)))
    sql(
        "INSERT INTO sentbox VALUES ('s', 1, 'w', ?, ?, 0.0)",
        (_index(105), _at(105)),
    )


def test_old_rosters_become_explicit() -> None:
    migration = _migration("5b1e8c27d4a9_group_state.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        _old_schema(conn, context)
        _alices_group(conn)
        with Operations.context(context):
            migration.upgrade()
        tables = set(sa.inspect(conn).get_table_names())
        assert {"groupstate", "groupwritten"} <= tables and not OLD_TABLES & tables
        rows = conn.exec_driver_sql("SELECT conversation_id, state, held, outgoing FROM groupstate").all()
        assert [row[0] for row in rows] == [1]
        _, blob, held, outgoing = rows[0]
        assert cbor2.loads(held) == [] and cbor2.loads(outgoing) == []
        group = state.load(blob)
        assert group.me == ALICE
        for owner in (ALICE, BOB):
            assert group.rosters.known(owner) == {0: ALICE, 1: BOB, 2: CAROL}
        assert group.rosters.known(CAROL) == {0: ALICE, 1: BOB, 2: CAROL, 3: DAVE}
        assert group.unnumbered == [(CAROL, 303)]
        assert set(group.readers) == {BOB, CAROL}
        assert group.readers[BOB].last == _index(208)
        assert group.readers[CAROL].last is None
        assert group.ledger.published == {BOB: _index(207)}
        assert group.ledger.acked_by == {BOB: 103}
        assert group.sent.check(105, _index(105))
        assert "frontier_index" not in {
            c["name"] for c in sa.inspect(conn).get_columns("readcapwal")
        }

        with Operations.context(context):
            migration.downgrade()
        tables = set(sa.inspect(conn).get_table_names())
        assert OLD_TABLES <= tables and not {"groupstate", "groupwritten"} & tables
        assert "acked_position" in {
            c["name"] for c in sa.inspect(conn).get_columns("conversationpeer")
        }
    engine.dispose()


def test_a_roster_that_cannot_be_told_is_left_out() -> None:
    """Bob inherited a roster from an Introduction alice has not read up
    to, and carol's base was never recorded: neither can be worked out."""
    migration = _migration("5b1e8c27d4a9_group_state.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        _old_schema(conn, context)
        sql = conn.exec_driver_sql
        sql("INSERT INTO writecapwal VALUES ('w', ?)", (bytes(32) + ALICE + _index(100),))
        sql("INSERT INTO conversation VALUES (1, 10, 'w')")
        sql("INSERT INTO rostermember VALUES (1, ?, ?, ?, NULL, NULL)",
            (ALICE, _at(101), cbor2.dumps([ALICE, BOB, CAROL])))
        sql("INSERT INTO rostermember VALUES (1, ?, NULL, NULL, ?, ?)",
            (BOB, ALICE, _at(150)))
        sql("INSERT INTO rostermember VALUES (1, ?, NULL, NULL, NULL, NULL)", (CAROL,))
        sql("INSERT INTO rostermember VALUES (1, ?, ?, NULL, ?, ?)",
            (DAVE, _at(-1 + 2**63), CAROL, _at(5)))
        sql("INSERT INTO acklevel VALUES (1, ?, ?, 9, ?, NULL)", (ALICE, _at(101), _at(1)))
        with Operations.context(context):
            migration.upgrade()
        (blob,) = conn.exec_driver_sql("SELECT state FROM groupstate").one()
        group = state.load(blob)
        assert group.rosters.known(ALICE) == {0: ALICE, 1: BOB, 2: CAROL}
        assert not group.rosters.known(BOB) and not group.rosters.known(CAROL)
        assert not group.rosters.known(DAVE)
    engine.dispose()
