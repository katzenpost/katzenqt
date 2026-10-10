"""group state

Revision ID: 5b1e8c27d4a9
Revises: 607a20224c70
Create Date: 2026-10-10 20:00:00.000000

A conversation's group state becomes one ``katzenqt.groupchat`` state
blob, with explicit rosters, and where each of our messages landed is noted
beside it until the blob next takes it in. Rosters kept by the old rules are worked out
here, once, with a frozen copy of those rules, and written as explicit
ones; the tables they were kept in are dropped. A downgrade restores the
old tables empty: explicit rosters cannot be turned back into the old
facts.
"""
from dataclasses import dataclass, field
from typing import Mapping, Sequence, Union

from alembic import op
import cbor2
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b1e8c27d4a9'
down_revision: Union[str, Sequence[str], None] = '607a20224c70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBSTREAM_NAME_PREFIX = ":substream:"


# The old roster rules, frozen as they stood before this revision.

@dataclass(frozen=True)
class _Inherited:
    introducer: bytes
    position: int


@dataclass
class _Old:
    bases: dict = field(default_factory=dict)
    seen: dict = field(default_factory=dict)
    introductions: dict = field(default_factory=dict)
    acks: dict = field(default_factory=dict)


def _grow(roster: Sequence[bytes], levels: Mapping[int, int], introductions: Mapping) -> list:
    by_introducer: dict = {}
    for (introducer, position), member in introductions.items():
        by_introducer.setdefault(introducer, []).append((position, member))
    added: list = []
    for index in sorted(levels):
        if not 0 <= index < len(roster):
            continue
        reached = levels[index]
        for position, member in sorted(by_introducer.get(roster[index], ())):
            if position > reached:
                break
            if member not in roster and member not in added:
                added.append(member)
    return added


def _timeline(old: _Old, member: bytes) -> list:
    steps = [(p, 0, levels, None) for (s, p), levels in old.acks.items() if s == member]
    steps.extend(
        (p, 1, None, introduced)
        for (introducer, p), introduced in old.introductions.items()
        if introducer == member
    )
    steps.sort(key=lambda step: step[:2])
    return [(p, levels, who) for p, _, levels, who in steps]


def _readable(old: _Old, roster: Sequence[bytes], levels: Mapping[int, int]) -> bool:
    return all(
        0 <= index < len(roster) and reached <= old.seen.get(roster[index], -1)
        for index, reached in levels.items()
    )


def _follow(old: _Old, member: bytes, through, visiting: frozenset):
    base = old.bases.get(member)
    if base is None or member in visiting:
        return None
    if isinstance(base, _Inherited):
        if old.seen.get(base.introducer, -1) < base.position:
            return None
        inherited = _follow(old, base.introducer, base.position, visiting | {member})
        if inherited is None or inherited[1]:
            return None
        start = inherited[0]
    else:
        start = base
    roster = list(start)
    unsettled: list = []
    have = old.seen.get(member, -1)
    for position, levels, introduced in _timeline(old, member):
        if through is not None and position > through:
            break
        if position > have:
            if levels is not None:
                unsettled.append((position, levels))
            continue
        if levels is not None:
            if unsettled or not _readable(old, roster, levels):
                unsettled.append((position, levels))
            else:
                roster.extend(_grow(roster, levels, old.introductions))
        elif not unsettled and introduced not in roster:
            roster.append(introduced)
    return tuple(roster[:256]), tuple(unsettled)


def _position(data) -> int:
    return -1 if data is None else int.from_bytes(data, "big")


def _counter(index: bytes) -> int:
    return int.from_bytes(index[:8], "little")


# The new state, built as plain data in the shape katzenqt.groupchat.state
# writes it (state version 1).

def _reader(key: bytes, read_cap: bytes, next_index: bytes, last) -> dict:
    return {
        "stream": key, "read_cap": read_cap, "next": next_index, "last": last,
        "kept": [], "mode": "idle", "scanned_to": -1,
    }


def _state(own_cap: bytes, entries, introductions, unnumbered, readers, published,
           acked_by, records) -> bytes:
    group = {
        "read_cap": own_cap,
        "policy": {"window": 3, "discard_acked": True, "keep_sent": None, "keep_received": None},
        "recheck": 4,
        "auto_scan": True,
        "rosters": {
            "entries": [
                [owner, [[i, {"key": k, "introduced": None, "at": -1}] for i, k in roster.items()]]
                for owner, roster in entries.items()
            ],
            "started": [],
            "introductions": [[[i, p], m] for (i, p), m in introductions.items()],
        },
        "readers": [[key, reader] for key, reader in readers.items()],
        "sent": {
            "records": [
                [at, {"index": index, "plaintext": None, "control": False, "written": 0}]
                for at, index in records.items()
            ],
            "backfilled": -1,
        },
        "ledger": {
            "published": [[k, v] for k, v in published.items()],
            "acked_by": [[k, v] for k, v in acked_by.items()],
            "heard": [],
            "induction": "idle",
            "invitee": None,
            "commit": False,
            "starts": [],
        },
        "epoch": 0,
        "pool": [own_cap[32:]],
        "tip": own_cap[32:],
        "deriving": False,
        "outbox": [],
        "pending": [],
        "tokens": 0,
        "unnumbered": [[i, p] for i, p in unnumbered],
        "introduced_at": None,
        "joined_through": None,
        "awaiting_start": [],
        "received": [],
    }
    return cbor2.dumps([1, group])


def _convert(conn) -> None:
    conversations = conn.execute(sa.text(
        "SELECT c.id, c.own_peer_id, w.write_cap, c.write_cap FROM conversation c "
        "JOIN writecapwal w ON w.id = c.write_cap WHERE w.write_cap IS NOT NULL"
    )).fetchall()
    for conv_id, own_peer_id, write_cap, stream in conversations:
        own_cap = write_cap[32:]
        me = own_cap[:32]
        rows = conn.execute(sa.text(
            "SELECT member_key, seen, base_roster, base_introducer, base_position "
            "FROM rostermember WHERE conversation_id = :c"), {"c": conv_id}).fetchall()
        if me not in {row[0] for row in rows}:
            continue
        old = _Old()
        for key, seen, base_roster, base_introducer, base_position in rows:
            old.seen[key] = _position(seen)
            if base_roster is not None:
                old.bases[key] = tuple(cbor2.loads(base_roster))
            elif base_introducer is not None:
                old.bases[key] = _Inherited(base_introducer, _position(base_position))
        for introducer, position, member in conn.execute(sa.text(
            "SELECT introducer_key, position, member_key FROM introductionseen "
            "WHERE conversation_id = :c AND position IS NOT NULL"), {"c": conv_id}):
            old.introductions[introducer, _position(position)] = member
        for acker, position, index, reached in conn.execute(sa.text(
            "SELECT acker_key, position, roster_index, reached FROM acklevel "
            "WHERE conversation_id = :c"), {"c": conv_id}):
            old.acks.setdefault((acker, _position(position)), {})[index] = _position(reached)

        entries = {}
        for key in old.bases:
            followed = _follow(old, key, None, frozenset())
            if followed is not None:
                entries[key] = dict(enumerate(followed[0]))
        own_roster = set(entries.get(me, {}).values())
        unnumbered = [
            at for at, member in old.introductions.items()
            if member not in own_roster and member != me
        ]

        readers, published, acked_by = {}, {}, {}
        for peer_id, name, active, acked_position, read_cap, next_index, last, acked in conn.execute(sa.text(
            "SELECT p.id, p.name, p.active, p.acked_position, r.read_cap, r.next_index, "
            "r.last_read_index, r.acked_index FROM conversationpeer p "
            "JOIN conversationpeerlink l ON l.conversation_peer_id = p.id "
            "JOIN readcapwal r ON r.id = p.read_cap_id WHERE l.conversation_id = :c"),
                {"c": conv_id}):
            if peer_id == own_peer_id or not active or name.startswith(SUBSTREAM_NAME_PREFIX):
                continue
            if read_cap is None or next_index is None:
                continue
            key = read_cap[:32]
            readers[key] = _reader(key, read_cap, next_index, last)
            if acked is not None:
                published[key] = acked
            if acked_position is not None:
                acked_by[key] = _position(acked_position)

        records = {
            _counter(index): index
            for (index,) in conn.execute(sa.text(
                "SELECT box_index FROM sentbox WHERE conversation_id = :c AND bacap_stream = :s"),
                {"c": conv_id, "s": stream})
        }
        state = _state(own_cap, entries, old.introductions, unnumbered, readers,
                       published, acked_by, records)
        conn.execute(sa.text(
            "INSERT INTO groupstate (conversation_id, state, held, outgoing) "
            "VALUES (:c, :s, :h, :o)"),
            {"c": conv_id, "s": state, "h": cbor2.dumps([]), "o": cbor2.dumps([])})


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('groupstate',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('state', sa.LargeBinary(), nullable=False),
    sa.Column('held', sa.LargeBinary(), nullable=False),
    sa.Column('outgoing', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_groupstate_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', name=op.f('pk_groupstate'))
    )
    op.create_table('groupwritten',
    sa.Column('pwal_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_groupwritten_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('pwal_id', name=op.f('pk_groupwritten'))
    )
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_groupwritten_conversation_id'), ['conversation_id'], unique=False)
    _convert(op.get_bind())

    with op.batch_alter_table('outgoingacks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outgoingacks_conversation_id'))
    op.drop_table('outgoingacks')
    with op.batch_alter_table('sentbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sentbox_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_sentbox_bacap_stream'))
    op.drop_table('sentbox')
    op.drop_table('rostermember')
    with op.batch_alter_table('introductionseen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_introductionseen_pending_pwal'))
        batch_op.drop_index(batch_op.f('ix_introductionseen_conversation_id'))
    op.drop_table('introductionseen')
    op.drop_table('acklevel')
    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.drop_column('acked_index')
        batch_op.drop_column('last_read_index')
        batch_op.drop_column('frontier_index')
    with op.batch_alter_table('conversationpeer', schema=None) as batch_op:
        batch_op.drop_column('acked_position')


def downgrade() -> None:
    """Downgrade schema. The old tables come back empty."""
    with op.batch_alter_table('conversationpeer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acked_position', sa.LargeBinary(), nullable=True))
    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('frontier_index', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('last_read_index', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('acked_index', sa.LargeBinary(), nullable=True))
    op.create_table('acklevel',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('acker_key', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=False),
    sa.Column('roster_index', sa.Integer(), nullable=False),
    sa.Column('reached', sa.LargeBinary(), nullable=False),
    sa.Column('value', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_acklevel_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'acker_key', 'position', 'roster_index', name=op.f('pk_acklevel'))
    )
    op.create_table('introductionseen',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('introducer_key', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=True),
    sa.Column('member_key', sa.LargeBinary(), nullable=False),
    sa.Column('pending_pwal', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_introductionseen_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_introductionseen'))
    )
    with op.batch_alter_table('introductionseen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_introductionseen_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_introductionseen_pending_pwal'), ['pending_pwal'], unique=False)
    op.create_table('rostermember',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('member_key', sa.LargeBinary(), nullable=False),
    sa.Column('seen', sa.LargeBinary(), nullable=True),
    sa.Column('base_roster', sa.LargeBinary(), nullable=True),
    sa.Column('base_introducer', sa.LargeBinary(), nullable=True),
    sa.Column('base_position', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_rostermember_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'member_key', name=op.f('pk_rostermember'))
    )
    op.create_table('sentbox',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('bacap_stream', sa.Uuid(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=False),
    sa.Column('written_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_sentbox_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sentbox')),
    sa.UniqueConstraint('bacap_stream', 'box_index', name='uniq_sentbox_stream_and_index')
    )
    with op.batch_alter_table('sentbox', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sentbox_bacap_stream'), ['bacap_stream'], unique=False)
        batch_op.create_index(batch_op.f('ix_sentbox_conversation_id'), ['conversation_id'], unique=False)
    op.create_table('outgoingacks',
    sa.Column('pwal_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('acker_key', sa.LargeBinary(), nullable=False),
    sa.Column('levels', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_outgoingacks_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('pwal_id', name=op.f('pk_outgoingacks'))
    )
    with op.batch_alter_table('outgoingacks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_outgoingacks_conversation_id'), ['conversation_id'], unique=False)
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_groupwritten_conversation_id'))
    op.drop_table('groupwritten')
    op.drop_table('groupstate')
