"""Version 5 of the katzenqt1 wire, read against the shared vectors:
pigeonhole_protocols' ``testdata/katzenqt1/groupchat_wire_vectors_v5.json``,
copied in unchanged. This client reads version 5 but does not write it, so
the vectors' packing (``start`` and ``added``) is not replayed."""

import json
from pathlib import Path
from typing import Any

import cbor2
import pytest
from sqlmodel import col, select

from katzenqt import acks, conversation_handlers, models, persistent, qt_models
from tests.test_conversation_handlers import _make_conversation
from tests.test_wire_v4 import _message_json

V = json.loads(
    (Path(__file__).parent / "data" / "groupchat_wire_vectors_v5.json").read_text()
)


@pytest.mark.parametrize("e", V["packed"], ids=[e["name"] for e in V["packed"]])
def test_packed_reads_as_its_vector(e: dict[str, Any]) -> None:
    cbor = bytes.fromhex(e["cbor"])
    gcm = models.GroupChatMessage.from_cbor(cbor)
    assert gcm.msg_type == models.GroupChatTypeEnum.PACKED
    assert gcm.texts == e["texts"]
    assert gcm.to_cbor() == cbor
    assert models.unsupported(cbor) is None
    message = acks.CODEC.decode(cbor)
    assert message is not None
    assert _message_json(message) == e["message"]


@pytest.mark.parametrize(
    "e", V["unsupported"], ids=[e["name"] for e in V["unsupported"]]
)
def test_unsupported_is_refused_and_named(e: dict[str, Any]) -> None:
    cbor = bytes.fromhex(e["cbor"])
    assert models.unsupported(cbor) == (e["version"], e["msg_type"])
    with pytest.raises(models.UnsupportedMessage) as exc:
        models.GroupChatMessage.from_cbor(cbor)
    assert (exc.value.version, exc.value.msg_type) == (e["version"], e["msg_type"])
    assert acks.CODEC.decode(cbor) is None


@pytest.mark.parametrize("e", V["malformed"], ids=[e["name"] for e in V["malformed"]])
def test_malformed_is_refused(e: dict[str, Any]) -> None:
    cbor = bytes.fromhex(e["cbor"])
    assert models.unsupported(cbor) is None
    with pytest.raises(ValueError):
        models.GroupChatMessage.from_cbor(cbor)
    assert acks.CODEC.decode(cbor) is None


def _shown(row: persistent.ConversationLog) -> str:
    notice = qt_models._notice_text(row.conversation_peer.name, row.payload[1:])
    return notice if notice is not None else qt_models._decode_group_chat_payload(row.payload).display


@pytest.mark.asyncio
async def test_a_conversation_shows_each_packed_text_a_ping_and_a_notice() -> None:
    h = bytes(32)
    boxes = [
        cbor2.dumps([5, h, 12, ["one", "two", "three"]]),
        cbor2.dumps([4, h, 11, None]),
        cbor2.dumps([6, h, 0, "from the future"]),
    ]
    async with persistent.asession() as sess:
        _conv, peer_id, _cap = await _make_conversation(sess, own_name="bob")
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        assert peer is not None
        for cbor in boxes:
            try:
                gcm: "models.GroupChatMessage | models.Unsupported" = models.GroupChatMessage.from_cbor(cbor)
            except models.UnsupportedMessage:
                gcm = models.Unsupported(cbor)
            result = await conversation_handlers.dispatch(sess, peer, gcm, b"F" + cbor)
            assert result.convlog_added
        await sess.commit()
        rows = (await sess.exec(
            select(persistent.ConversationLog).order_by(col(persistent.ConversationLog.conversation_order))
        )).all()
        assert [_shown(r) for r in rows] == [
            "one", "two", "three", "bob pinged",
            "bob sent a message this app cannot show (type 0, version 6): update the app",
        ]
