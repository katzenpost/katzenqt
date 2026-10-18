"""The members ``groupchat_edges`` drives, checked here and recorded for
CryptWalker's replay."""

from __future__ import annotations

import io
from pathlib import Path

from katzenqt.groupchat.events import Send
from katzenqt.groupchat.wire import ROSTER_MAX, counter
from tests.groupchat_edges import (
    EDGES,
    bounded_state,
    client_writes,
    record,
    retired_prefix,
    roster_full,
)


def test_numbering_stops_where_the_roster_ends() -> None:
    group = roster_full(io.StringIO())
    numbered = sorted(group.rosters.known(group.me))
    assert numbered[-1] == ROSTER_MAX - 1
    assert len(numbered) == ROSTER_MAX
    assert len(group.unnumbered) == 2
    assert not group.room
    assert group.compose() == []


def test_a_retired_place_names_no_member() -> None:
    group = retired_prefix(io.StringIO())
    assert not any(key.startswith(b"retired") for key in group.rosters.keys)
    bob = next(key for key in group.readers)
    assert group.rosters.resolve(bob, 3) is None
    assert all(not key.startswith(b"retired") for key in group.ledger.heard)


def test_what_a_member_remembers_stays_bounded() -> None:
    group = bounded_state(io.StringIO())
    assert group.received == {}
    bob = next(r for r in group.readers.values() if r.passed == 3)
    here = counter(bob.next)
    assert bob.tried and all(t > here for t in bob.tried)
    heard = set(group.ledger.heard[bob.stream].values())
    assert set(bob.tried) <= heard


def test_a_member_whose_client_writes_writes_nothing_itself() -> None:
    group = client_writes(io.StringIO())
    start = group.read_cap[32:]
    (record_,) = group.sent.records.values()
    assert record_.index == start
    message = group.codec.decode(record_.plaintext or b"")
    assert message is not None and message.content == b"hi"
    assert group.pool == [start] and not group.deriving
    assert group.handle(Send(b"more")) == []


def test_every_edge_is_recorded(tmp_path: Path) -> None:
    record(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"edge-{name}.jsonl" for name in EDGES
    )
