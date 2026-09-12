"""Canonical membership hash and advisory verification tests."""
from __future__ import annotations

import hashlib
import uuid

import pytest

from katzenqt import conversation_handlers, models, persistent
from katzenqt.models import GroupChatMessage, SendOperation


def test_canonical_hash_is_deduped_sorted_and_domained():
    a = b"a" * 136
    b = b"b" * 136
    got = models.canonical_membership_hash([b, a, a])
    want = hashlib.sha256(models.MEMBERSHIP_DOMAIN + a + b).digest()
    assert got == want
    assert models.canonical_membership_hash([a, b]) == got
    assert not models.is_membership_sentinel(got)


def test_sentinels():
    assert models.is_membership_sentinel(b"TODO" * 8)
    assert models.is_membership_sentinel(bytes(32))
    assert not models.is_membership_sentinel(b"x" * 32)


async def _make_conversation(name: str = "demo") -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x02" * 168, next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcapwal.id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(name=name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=rcapwal.id, active=True, conversation=convo,
    )
    convo.own_peer = own_peer
    peer_rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x01" * 136, next_index=b"\x01" * 104,
    )
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(peer_rcw)
        sess.add(persistent.ConversationPeer(
            name="alice", read_cap_id=peer_rcw.id, active=True, conversation=convo,
        ))
        await sess.commit()
        await sess.refresh(convo)
        return convo.id


@pytest.mark.asyncio
async def test_local_membership_hash_uses_write_cap_and_peers():
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        got = await conversation_handlers.local_membership_hash(sess, convo)
    want = models.canonical_membership_hash([b"\x02" * 136, b"\x01" * 136])
    assert got == want
    assert not models.is_membership_sentinel(got)


@pytest.mark.asyncio
async def test_send_stamps_the_real_membership_hash():
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        expected = await conversation_handlers.local_membership_hash(sess, convo)
        gcm = GroupChatMessage(
            version=0, membership_hash=b"TODO" * 8, text="hello",
        )
        gcm.membership_hash = expected
        own_bacap_stream = convo.write_cap

    send_op = SendOperation(bacap_stream=own_bacap_stream, messages=[gcm])
    _, db_entries = send_op.serialize(chunk_size=1530, conversation_id=conv_id)
    final = [
        e for e in db_entries
        if isinstance(e, persistent.PlaintextWAL)
        and e.bacap_payload[:1] == b"F"
    ]
    assert final, "no final chunk on the wire"
    sent = GroupChatMessage.from_cbor(final[-1].bacap_payload[1:])
    assert sent.text == "hello"
    assert not models.is_membership_sentinel(sent.membership_hash)
    assert sent.membership_hash == expected


def test_shared_membership_vectors() -> None:
    import json
    from pathlib import Path

    corpus = json.loads(
        (Path(__file__).parent / "vectors/membership_vectors.json")
        .read_text(encoding="utf-8")
    )
    for case in corpus["cases"]:
        caps = [bytes.fromhex(value) for value in case["caps"]]
        assert models.canonical_membership_hash(caps).hex() == case["expected"]
