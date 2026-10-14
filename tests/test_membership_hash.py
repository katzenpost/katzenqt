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

from katzenqt import acks, conversation_handlers, models, persistent
from katzenqt.headless import _actions, _args
from katzenqt.models import GroupChatMessage


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
        for name, active in (("paused", False), (models.SUBSTREAM_NAME_PREFIX + "x", True)):
            rcw = persistent.ReadCapWAL(
                id=uuid.uuid4(), read_cap=bytes([len(name)]) * 136, next_index=bytes(104),
            )
            sess.add(rcw)
            sess.add(persistent.ConversationPeer(
                name=name, read_cap_id=rcw.id, active=active, conversation=convo,
            ))
        await sess.commit()
        await sess.refresh(convo)
        got = await conversation_handlers.local_membership_hash(sess, convo)
    want = models.canonical_membership_hash([b"\x02" * 136, b"\x01" * 136])
    assert got == want
    assert not models.is_membership_sentinel(got)


@pytest.mark.asyncio
async def test_a_conversation_awaiting_its_write_cap_still_counts_itself(
) -> None:
    own_rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x07" * 136, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(
        name="unprovisioned", write_cap=None, first_unread=0,
    )
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=own_rcw.id, active=True, conversation=convo,
    )
    convo.own_peer = own_peer
    peer_rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x08" * 136, next_index=b"\x00" * 104,
    )
    async with persistent.asession() as sess:
        sess.add(own_rcw)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(peer_rcw)
        sess.add(persistent.ConversationPeer(
            name="bob", read_cap_id=peer_rcw.id, active=True,
            conversation=convo,
        ))
        await sess.commit()
        await sess.refresh(convo)
        got = await conversation_handlers.local_membership_hash(sess, convo)
    assert got == models.canonical_membership_hash(
        [b"\x07" * 136, b"\x08" * 136],
    )


@pytest.mark.asyncio
async def test_send_stamps_the_real_membership_hash() -> None:
    conv_id = await _make_conversation()
    gcm = GroupChatMessage(version=0, membership_hash=b"TODO" * 8, text="hello")
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        expected = await conversation_handlers.local_membership_hash(sess, convo)
        _, db_entries = await acks.serialize_with_acks(sess, convo, gcm)
    final = [
        e for e in db_entries
        if isinstance(e, persistent.PlaintextWAL)
        and e.bacap_payload[:1] == b"F"
    ]
    assert final, "no final chunk on the wire"
    sent = GroupChatMessage.from_cbor(final[-1].bacap_payload[1:])
    assert sent.text == "hello"
    assert sent.membership_hash == expected


@pytest.mark.asyncio
async def test_headless_prints_the_membership_hash(caplog: pytest.LogCaptureFixture) -> None:
    conv_id = await _make_conversation("hashed")
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        expected = await conversation_handlers.local_membership_hash(sess, convo)
    with caplog.at_level(logging.INFO):
        assert await _actions._action_membership_hash(
            _args.MembershipHash(action="membership-hash", conv_name="hashed")
        ) == 0
        assert await _actions._action_membership_hash(
            _args.MembershipHash(action="membership-hash", conv_name="missing")
        ) == 2
    assert f"MEMBERSHIP_HASH={expected.hex()}" in caplog.text


@pytest.mark.asyncio
async def test_an_introduction_is_checked_against_the_group_before_its_member(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conv_id = await _make_conversation()
    me, alice = b"\x02" * 136, b"\x01" * 136

    def introducing(read_cap: bytes, members: list[bytes]) -> GroupChatMessage:
        return GroupChatMessage(
            version=models.GROUP_CHAT_VERSION,
            membership_hash=models.canonical_membership_hash(members),
            msg_type=models.GroupChatTypeEnum.INTRODUCTION,
            introduction=models.GroupChatPleaseAdd(display_name="x", read_cap=read_cap),
        )

    async with persistent.asession() as sess:
        peer = (await conversation_handlers._conversation_peers(sess, conv_id))[0]
        with caplog.at_level(logging.INFO):
            await conversation_handlers._verify_membership_advisory(
                sess, peer, introducing(me, [alice]),
            )
            await conversation_handlers._verify_membership_advisory(
                sess, peer, introducing(b"\x05" * 136, [me, alice]),
            )
            assert "mismatch" not in caplog.text
            await conversation_handlers._verify_membership_advisory(
                sess, peer, GroupChatMessage(
                    version=models.GROUP_CHAT_VERSION,
                    membership_hash=models.canonical_membership_hash([alice]), text="hi",
                ),
            )
    assert "mismatch" in caplog.text
