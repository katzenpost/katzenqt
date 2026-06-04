"""Guards that stop a client joining a conversation more than once.

A voucher is a join operation; minting a second one for a conversation the
client already belongs to (or already has a voucher in flight for) re-runs the
handshake and duplicates every member and message. These tests pin the helper
layer that the GUI relies on to prevent that.
"""
from __future__ import annotations

import uuid

import pytest

from katzenqt import network, persistent, voucher


async def _make_conversation(name: str = "demo", own: str = "me") -> int:
    """A freshly created conversation: own (inactive) peer only, no members."""
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcapwal.id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(name=name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name=own, read_cap_id=rcapwal.id, active=False, conversation=convo,
    )
    convo.own_peer = own_peer
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        await sess.commit()
        await sess.refresh(convo)
        return convo.id


async def _add_active_peer(conversation_id: int, name: str) -> None:
    rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x01" * 136, next_index=b"\x01" * 104,
    )
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        sess.add(rcw)
        sess.add(persistent.ConversationPeer(
            name=name, read_cap_id=rcw.id, active=True, conversation=conv,
        ))
        await sess.commit()


async def _add_pending(conversation_id: int) -> uuid.UUID:
    async with persistent.asession() as sess:
        pv = persistent.PendingVoucher(
            role="joiner", conversation_id=conversation_id,
            step="awaiting", voucher=b"v" * 32,
        )
        sess.add(pv)
        await sess.commit()
        await sess.refresh(pv)
        return pv.id


@pytest.mark.asyncio
async def test_fresh_conversation_is_not_joined():
    conv_id = await _make_conversation()
    assert await voucher.conversation_is_joined(conv_id) is False


@pytest.mark.asyncio
async def test_active_member_counts_as_joined():
    conv_id = await _make_conversation()
    await _add_active_peer(conv_id, "alice")
    assert await voucher.conversation_is_joined(conv_id) is True


@pytest.mark.asyncio
async def test_substream_peer_does_not_count_as_joined():
    conv_id = await _make_conversation()
    await _add_active_peer(conv_id, f"{network._SUBSTREAM_NAME_PREFIX}1:ab")
    assert await voucher.conversation_is_joined(conv_id) is False


@pytest.mark.asyncio
async def test_mint_refuses_when_already_joined():
    conv_id = await _make_conversation()
    await _add_active_peer(conv_id, "alice")
    with pytest.raises(voucher.AlreadyJoinedError):
        await voucher.mint_and_publish(None, conv_id, "me")


@pytest.mark.asyncio
async def test_mint_refuses_when_voucher_pending():
    conv_id = await _make_conversation()
    await _add_pending(conv_id)
    with pytest.raises(voucher.PendingVoucherExistsError):
        await voucher.mint_and_publish(None, conv_id, "me")


@pytest.mark.asyncio
async def test_pending_lookup_list_and_cancel():
    conv_id = await _make_conversation()
    assert await voucher.pending_voucher_for(conv_id) is None
    pv_id = await _add_pending(conv_id)
    assert await voucher.pending_voucher_for(conv_id) == pv_id
    assert any(row[0] == pv_id for row in await voucher.list_pending_vouchers())
    await voucher.cancel_pending_voucher(pv_id)
    assert await voucher.pending_voucher_for(conv_id) is None
