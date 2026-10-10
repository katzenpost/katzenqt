"""Edges of the group chat state machine and its simulator that no scenario
reaches."""

from __future__ import annotations

import cbor2
import pytest

from katzenqt.groupchat import events, sim, state, wire
from katzenqt.groupchat.group import Group
from katzenqt.groupchat.numbering import Rosters


def test_a_malformed_message_is_ignored() -> None:
    assert wire.CborCodec().decode(b"\xa1\x01") is None


def test_unknown_events_and_streams_do_nothing() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.group("alice")
    stranger = b"\x01" * 32
    assert alice.handle(object()) == []  # type: ignore[arg-type]
    assert alice.handle(events.Read(stranger)) == []
    assert alice.handle(events.Scan(stranger)) == []
    effects = alice.handle(events.BoxRead(stranger, bytes(104), events.Missing.NOT_FOUND))
    assert all(isinstance(effect, events.DeriveIndexes) for effect in effects)
    assert alice.observe(stranger, bytes(104), None, bytes(104)) == []
    assert alice.handle(events.IndexesDerived("jump", stranger, ())) == []


def test_a_stale_read_is_ignored() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    bob = world.members["bob"].key
    stale = events.BoxRead(bob, bytes(104), b"late", bytes(104))
    effects = world.group("alice").handle(stale)
    assert all(isinstance(effect, events.DeriveIndexes) for effect in effects)


def test_an_undecodable_box_is_passed_over() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.group("alice")
    reader = alice.readers[world.members["bob"].key]
    alice.handle(events.Read(reader.stream))
    effects = alice.handle(
        events.BoxRead(reader.stream, reader.next, b"\xff", bytes(104))
    )
    assert not any(isinstance(effect, events.Deliver) for effect in effects)
    assert reader.next == bytes(104)


def test_malformed_acks_are_dropped() -> None:
    codec = wire.CborCodec()
    plaintext = cbor2.dumps({"Version": 1, "Content": b"x", "Acks": b"\x01\x02"})
    message = codec.decode(plaintext)
    assert message is not None and message.acks == {}


def test_a_second_read_waits_for_the_first() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.group("alice")
    bob = world.members["bob"].key
    assert alice.handle(events.Read(bob))
    assert alice.handle(events.Read(bob)) == []


def test_numbering_waits_for_the_introducer() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.group("alice")
    stranger, newcomer = b"\x01" * 32, b"\x02" * 32
    alice.rosters.introductions[stranger, 5] = newcomer
    alice.unnumbered.append((stranger, 5))
    bob = world.members["bob"].key
    alice.rosters.introductions[bob, 3] = b"\x03" * 32
    alice.unnumbered.append((bob, 3))
    world.run("send alice hi")
    assert alice.unnumbered == [(stranger, 5)]


def test_numbering_through_an_unknown_introducer() -> None:
    rosters = Rosters()
    rosters.found([b"a"])
    rosters.number(b"a", 1, None, at=3)
    assert rosters.resolve(b"a", 1) is None
    assert rosters.resolve(b"a", 7) is None


def test_retiring_renames_every_mention() -> None:
    rosters = Rosters()
    rosters.found([b"a", b"b"])
    rosters.introductions[b"b", 4] = b"c"
    rosters.number(b"a", 2, (b"b", 4), at=1)
    rosters.started[b"c"] = (b"b", 4)
    rosters.retire(b"b", b"gone")
    assert rosters.entries[b"a"][2].introduced == (b"gone", 4)
    assert rosters.started[b"c"] == (b"gone", 4)
    assert rosters.resolve(b"a", 2) == b"c"
    assert b"gone" in rosters.entries


def test_the_replica_refuses_a_bad_signature() -> None:
    world = sim.World()
    world.run("new-group alice")
    stored = sim.Stored(bytes(32), b"payload", bytes(64))
    assert world.replica.write(stored) == "bad signature"


def test_bad_commands() -> None:
    world = sim.World()
    with pytest.raises(ValueError, match="unknown command"):
        world.run("frobnicate")
    world.run("new-group alice")
    with pytest.raises(ValueError, match="before new-group"):
        world.run("option window 2")


def test_where_and_who_for_unknown_positions() -> None:
    world = sim.World()
    world.run("new-group alice")
    assert world.where(bytes(104)) == "?"
    assert world.name_of(None) == "?"
    assert world.introduced(0) == "?"
    world.run("new-group bob")
    assert world.run("acks alice bob 0") == ["not read"]


def test_a_founder_alone() -> None:
    alice = sim.World()
    alice.run("new-group alice")
    group: Group = alice.group("alice")
    assert group.rosters.known(group.me) == {0: group.me}


def test_unknown_tokens_are_ignored() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.group("alice")
    alice.written(99, bytes(104))
    alice.abandoned(99)
    assert alice.pending == {}


def test_abandoning_restores_the_earlier_ack() -> None:
    world = sim.World()
    world.run("option passive on")
    world.run("option delay-writes on")
    world.run("new-group alice bob")
    for line in ("send bob b0", "land bob", "read alice bob", "send alice a0", "land alice"):
        world.run(line)
    bob = world.members["bob"].key
    alice = world.group("alice")
    first = alice.ledger.published[bob]
    for line in ("send bob b1", "land bob", "read alice bob", "send alice upload", "abandon alice"):
        world.run(line)
    assert alice.ledger.published[bob] == first


def test_an_induction_can_be_called_off() -> None:
    world = sim.World()
    world.run("option passive on")
    world.run("new-group alice bob")
    alice = world.group("alice")
    alice.begin_induction()
    assert alice.ledger.holding
    alice.cancel_induction()
    assert not alice.ledger.holding


def test_passive_reads_of_a_stream_not_followed() -> None:
    world = sim.World()
    world.run("option passive on")
    world.run("new-group alice bob")
    world.run("new-group carol")
    assert world.run("read alice carol") == []
    assert world.run("send alice hi") == ['alice@0 "hi"']


def test_state_that_cannot_be_read() -> None:
    with pytest.raises(state.StateError):
        state.load(b"\xff")
    with pytest.raises(state.StateError, match="version 7"):
        state.load(cbor2.dumps([7, {}]))
