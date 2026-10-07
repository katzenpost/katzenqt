"""Membership hash: canonical recipe and send stamping.

The canonical hash is fixed so independent implementations agree on the
wire, and a sent message must now carry that hash rather than the legacy
TODO sentinel.
"""
from __future__ import annotations

import hashlib
import logging
import uuid

import pytest
from sqlmodel import select

from katzenqt import conversation_handlers, models, persistent, voucher
from katzenqt.tally import events as tally_events
from katzenqt.tally import send as tally_send
from katzenqt.models import GroupChatMessage, SendOperation


def test_canonical_hash_is_deduped_sorted_and_domained() -> None:
    a = b"a" * 136
    b = b"b" * 136
    got = models.canonical_membership_hash([b, a, a])
    want = hashlib.sha256(models.MEMBERSHIP_DOMAIN + a[:32] + b[:32]).digest()
    assert got == want
    assert models.canonical_membership_hash([a, b]) == got
    assert not models.is_membership_sentinel(got)


def test_canonical_hash_ignores_the_read_cap_index_suffix() -> None:
    """Two caps for the same member (same 32-byte key, different 104-byte
    index suffix) must collapse to one member, so membership agrees across a
    joiner's pre-mutation cap, the salt-mutated cap the group holds, and a
    future-only cap starting at a later index."""
    key = b"k" * 32
    own_copy = key + b"\x01" * 104
    shared_copy = key + b"\x02" * 104
    other = b"z" * 32 + b"\x03" * 104

    assert (
        models.canonical_membership_hash([own_copy, other])
        == models.canonical_membership_hash([shared_copy, other])
    )
    # Both copies of one member count once, not twice.
    assert (
        models.canonical_membership_hash([own_copy, shared_copy])
        == models.canonical_membership_hash([own_copy])
    )


def test_sentinels() -> None:
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
async def test_local_membership_hash_uses_write_cap_and_peers() -> None:
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        got = await conversation_handlers.local_membership_hash(sess, convo)
    want = models.canonical_membership_hash([b"\x02" * 136, b"\x01" * 136])
    assert got == want
    assert not models.is_membership_sentinel(got)


@pytest.mark.asyncio
async def test_send_stamps_the_real_membership_hash() -> None:
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
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


_JOINER_CAP = b"\x07" * 136


async def _add_joiner(conv_id: int) -> None:
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        rcw = persistent.ReadCapWAL(
            id=uuid.uuid4(), read_cap=_JOINER_CAP, next_index=b"\x00" * 104,
        )
        sess.add(rcw)
        sess.add(persistent.ConversationPeer(
            name="bob", read_cap_id=rcw.id, active=True, conversation=convo,
        ))
        await sess.commit()


@pytest.mark.asyncio
async def test_local_membership_hash_can_exclude_one_member() -> None:
    conv_id = await _make_conversation("excl")
    await _add_joiner(conv_id)
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        full = await conversation_handlers.local_membership_hash(sess, convo)
        without = await conversation_handlers.local_membership_hash(
            sess, convo, exclude_read_cap=_JOINER_CAP,
        )
    assert full == models.canonical_membership_hash(
        [b"\x02" * 136, b"\x01" * 136, _JOINER_CAP],
    )
    assert without == models.canonical_membership_hash(
        [b"\x02" * 136, b"\x01" * 136],
    )


@pytest.mark.asyncio
async def test_introduction_carries_the_group_before_the_addition() -> None:
    conv_id = await _make_conversation("intro")
    await _add_joiner(conv_id)
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        pre_add = await conversation_handlers.local_membership_hash(
            sess, convo, exclude_read_cap=_JOINER_CAP,
        )
        post_add = await conversation_handlers.local_membership_hash(
            sess, convo,
        )
    await voucher._write_introduction_log(conv_id, "bob", _JOINER_CAP)
    async with persistent.asession() as sess:
        pwals = (await sess.exec(
            select(persistent.PlaintextWAL).where(
                persistent.PlaintextWAL.conversation_id == conv_id
            )
        )).all()
    finals = [p for p in pwals if p.bacap_payload[:1] == b"F"]
    assert len(finals) == 1
    gcm = GroupChatMessage.from_cbor(finals[0].bacap_payload[1:])
    assert gcm.msg_type is models.GroupChatTypeEnum.INTRODUCTION
    assert gcm.introduction is not None
    assert gcm.introduction.read_cap == _JOINER_CAP
    assert gcm.membership_hash == pre_add
    assert gcm.membership_hash != post_add
    assert not models.is_membership_sentinel(gcm.membership_hash)


@pytest.mark.asyncio
async def test_tally_send_replaces_the_placeholder_with_a_real_hash() -> None:
    conv_id = await _make_conversation("tallyhash")
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        expected = await conversation_handlers.local_membership_hash(
            sess, convo,
        )
        gcm = tally_events.build_create(b"s" * 32, b"state")
        assert models.is_membership_sentinel(gcm.membership_hash)
        await tally_send.stage_outbound(sess, convo, gcm)
        await sess.commit()
    assert gcm.membership_hash == expected
    assert not models.is_membership_sentinel(gcm.membership_hash)


@pytest.mark.asyncio
async def test_tally_send_leaves_a_deliberate_hash_alone() -> None:
    conv_id = await _make_conversation("tallykeep")
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        gcm = tally_events.build_create(b"s" * 32, b"state")
        gcm.membership_hash = bytes(range(32))
        await tally_send.stage_outbound(sess, convo, gcm)
        await sess.commit()
    assert gcm.membership_hash == bytes(range(32))
