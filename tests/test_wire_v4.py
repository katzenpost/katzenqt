"""Version 4 of the katzenqt1 wire, read against the shared vectors:
pigeonhole_protocols' ``testdata/katzenqt1/groupchat_wire_vectors_v4.json``,
copied in unchanged."""

import json
from pathlib import Path

from typing import Any

import pytest

from katzenqt import acks, models
from katzenqt.groupchat import wire

V = json.loads(
    (
        Path(__file__).parent / "data" / "groupchat_wire_vectors_v4.json"
    ).read_text()
)


def _hex(b: "bytes | None") -> "str | None":
    return None if b is None else b.hex()


def _message_json(m: wire.Message) -> dict[str, Any]:
    return {
        "content": _hex(m.content),
        "introduction": None
        if m.introduction is None
        else {
            "name": m.introduction.name,
            "read_cap": m.introduction.read_cap.hex(),
        },
        "numbers": [[i, _hex(p)] for i, p in m.numbers],
        "introduced_at": None
        if m.introduced_at is None
        else {
            "introducer": m.introduced_at.introducer,
            "index": m.introduced_at.index.hex(),
        },
        "acks": [[k, v] for k, v in sorted(m.acks.items())],
        "membership_hash": _hex(m.membership_hash),
    }


@pytest.mark.parametrize(
    "e", V["messages"], ids=[e["name"] for e in V["messages"]]
)
def test_message_reads_as_its_vector(e: dict[str, Any]) -> None:
    message = acks.CODEC.decode(bytes.fromhex(e["cbor"]))
    assert message is not None
    assert _message_json(message) == e["message"]
    if e["rewritten"] == e["cbor"]:
        # What the member keeps of a box is written back as it was read.
        assert (
            models.GroupChatMessage.from_cbor(bytes.fromhex(e["cbor"]))
            .to_cbor()
            .hex()
            == e["cbor"]
        )


@pytest.mark.parametrize(
    "e", V["written"], ids=[e["name"] for e in V["written"]]
)
def test_written_box_reads_as_its_vector(e: dict[str, Any]) -> None:
    box = bytes.fromhex(e["box"])
    assert box[:1] == b"F"
    message = acks.CODEC.decode(box[1:])
    assert message is not None
    want = dict(e["message"])
    if want["content"] is not None:
        # Content travels as the whole box, the acks written with it included.
        want["content"] = box[1:].hex()
        want["membership_hash"] = None
    assert _message_json(message) == want


@pytest.mark.parametrize(
    "e", V["replies"], ids=[e["name"] for e in V["replies"]]
)
def test_reply_carries_its_join_reply(e: dict[str, Any]) -> None:
    reply = acks._join_reply(
        models.GroupChatReplyWho.from_cbor(bytes.fromhex(e["cbor"]))
    )
    want = e["join_reply"]
    if want is None:
        assert reply is None
        return
    assert reply is not None
    assert [
        None if m is None else [m[0].hex(), m[1].hex()] for m in reply.members
    ] == want["members"]
    assert [
        None if r is None else [list(x) for x in r] for r in reply.rosters
    ] == want["rosters"]
    assert [
        [by, index.hex(), m] for by, index, m in reply.introductions
    ] == want["introductions"]
    assert (reply.introducer, reply.newcomer) == (
        want["introducer"],
        want["newcomer"],
    )


@pytest.mark.parametrize(
    "e", V["malformed"], ids=[e["name"] for e in V["malformed"]]
)
def test_malformed_array_is_refused(e: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        models.GroupChatMessage.from_cbor(bytes.fromhex(e["cbor"]))
    assert acks.CODEC.decode(bytes.fromhex(e["cbor"])) is None
