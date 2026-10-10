"""Edges of the group chat simulator that no scenario reaches."""

from __future__ import annotations

import cbor2
import pytest

from katzenqt.groupchat import member, sim


def test_malformed_acks_are_ignored() -> None:
    plaintext = cbor2.dumps({"Version": 1, "Acks": b"\x01\x02"})
    assert member.Message.decode(plaintext).acks == {}


def test_unhandled_event() -> None:
    world = sim.World()
    world.run("new-group alice")
    with pytest.raises(TypeError):
        world.members["alice"].handle(object())  # type: ignore[arg-type]


def test_unknown_streams() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    alice = world.members["alice"]
    index = alice.cap.message_box_index
    assert alice.resolve(b"\x00" * 32, 0) == {}
    assert not alice.on_stream(b"\x00" * 32, index)
    assert world.owner_of(bytes(104)) == "?"


def test_replica_refuses_a_bad_signature() -> None:
    world = sim.World()
    world.run("new-group alice")
    (write, _) = world.members["alice"].handle(member.Send("hi"))
    assert isinstance(write, member.Write)
    forged = member.Write(
        write.position, write.box_id, write.payload, bytes(64)
    )
    assert world.replica.write(forged) == "bad signature"


def test_bad_commands() -> None:
    world = sim.World()
    with pytest.raises(ValueError, match="unknown command"):
        world.run("frobnicate")
    world.run("new-group alice")
    with pytest.raises(ValueError, match="before new-group"):
        world.run("option redact on")


def test_a_stale_fetch_result_is_ignored() -> None:
    world = sim.World()
    world.run("new-group alice bob")
    world.run("send alice hi")
    alice = world.members["alice"].key
    stale = member.Found(alice, 12345, member.Stored(b"", b"", b""))
    assert world.members["bob"].handle(stale) == []
