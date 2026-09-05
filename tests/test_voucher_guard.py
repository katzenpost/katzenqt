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
from sqlmodel import select


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


@pytest.mark.asyncio
async def test_resume_picks_awaiting_joiner_only():
    """A restart resumes precisely the joiner handshakes the DB can still
    continue: ``awaiting`` ones (box-1 index persisted). A ``minted`` one has
    no box-1 index yet (and its box 0 is already written, so re-minting would
    duplicate it) and an ``inducting`` one is the *inductor*'s row, whose
    joiner-side partner is someone else's problem."""
    awaiting = await _make_conversation("awaiting")
    minted = await _make_conversation("minted")
    inducting = await _make_conversation("inducting")
    done = await _make_conversation("done")
    for conv_id in (awaiting, minted, done):
        async with persistent.asession() as sess:
            pv = persistent.PendingVoucher(
                role="joiner", conversation_id=conv_id,
                step="awaiting" if conv_id == awaiting else (
                    "minted" if conv_id == minted else "done"
                ),
                voucher=b"v" * 32,
                box1_index=b"\x00" * 104 if conv_id in (awaiting, done) else None,
            )
            sess.add(pv)
            await sess.commit()
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role="inductor", conversation_id=inducting,
            step="inducting", voucher=b"w" * 32,
        ))
        await sess.commit()
    assert await voucher.pending_joiner_join_conversation_ids() == [awaiting]


@pytest.mark.asyncio
async def test_resume_empty_when_no_join_in_flight():
    await _make_conversation()
    assert await voucher.pending_joiner_join_conversation_ids() == []


@pytest.mark.asyncio
async def test_intro_announcement_emits_increment(monkeypatch):
    """The sender's own 'alice added bob' row must signal the UI tally.

    ConversationLogModel grows ``row_count`` by one per ``False`` update
    event and fetches rows by ``conversation_order == index_row``. Every
    other path that appends a ConversationLog row emits the event; the own
    announcement row was the only one that didn't, leaving the sender's view
    permanently short by one message per announcement.
    """
    conversation_id = await _make_conversation()

    async def _noop():
        return None

    monkeypatch.setattr(voucher, "check_for_new", _noop)
    monkeypatch.setattr(voucher, "_wait_intro_acked", lambda *a, **k: _noop())

    while not network.conversation_update_queue.empty():
        network.conversation_update_queue.get_nowait()

    await voucher.send_introduction_message(conversation_id, "bob", b"\x02" * 136)

    events = []
    while not network.conversation_update_queue.empty():
        events.append(network.conversation_update_queue.get_nowait())
    assert events == [(conversation_id, False)]

    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        rows = (await sess.exec(
            select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == conversation_id,
            )
        )).all()
    assert len(rows) == 1
    assert rows[0].conversation_peer_id == conv.own_peer_id
    assert rows[0].conversation_order == 0
    assert rows[0].payload.startswith(b"F")
