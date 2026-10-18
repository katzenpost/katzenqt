"""The members ``groupchat_edges`` drives, checked here and recorded for
CryptWalker's replay."""

from __future__ import annotations

import io
from pathlib import Path

from katzenqt.groupchat.wire import ROSTER_MAX
from tests.groupchat_edges import EDGES, record, retired_prefix, roster_full


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


def test_every_edge_is_recorded(tmp_path: Path) -> None:
    record(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"edge-{name}.jsonl" for name in EDGES
    )
