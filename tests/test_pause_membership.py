from uuid import UUID

import pytest
from sqlmodel import select

from katzenqt import conversation_handlers, network, persistent, voucher
from tests.test_membership_hash import _make_conversation


@pytest.mark.asyncio
async def test_read_pause_preserves_membership_and_voucher_members() -> None:
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        assert conv is not None
        peer = next(p for p in conv.peers if p.id != conv.own_peer_id)
        stream = peer.read_cap_id
        before_hash = await conversation_handlers.local_membership_hash(sess, conv)
    before_reply = await voucher._build_who_reply(conv_id)
    await network.pause_peer_reads(bacap_stream=stream)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        assert conv is not None
        assert await conversation_handlers.local_membership_hash(sess, conv) == before_hash
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None and rcw.read_paused
        assert all(p.active for p in conv.peers)
    assert await voucher._build_who_reply(conv_id) == before_reply
    await network.resume_peer_reads(bacap_stream=stream)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None and not rcw.read_paused
    assert await voucher._build_who_reply(conv_id) == before_reply


@pytest.mark.asyncio
async def test_pause_is_persisted_before_inflight_cancellation() -> None:
    import asyncio

    stream = UUID(int=100)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=stream))
        sess.add(persistent.ConversationPeer(name="bob", read_cap_id=stream))
        await sess.commit()
    entered = asyncio.Event()
    checked = asyncio.Event()

    async def reader() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            async with persistent.asession() as sess:
                rcw = await sess.get(persistent.ReadCapWAL, stream)
                assert rcw is not None and rcw.read_paused
                peer = (await sess.exec(select(persistent.ConversationPeer))).one()
                assert peer.active
                checked.set()

    task = asyncio.create_task(reader())
    network._inflight_reads[stream] = task
    await asyncio.wait_for(entered.wait(), timeout=2)
    await network.pause_peer_reads(bacap_stream=stream)
    assert task.cancelled()
    assert checked.is_set()


@pytest.mark.asyncio
async def test_pause_does_not_resurrect_a_finished_transfer() -> None:
    stream = UUID(int=102)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=stream))
        sess.add(persistent.ConversationPeer(
            name=":substream:1:done", read_cap_id=stream, active=False,
        ))
        await sess.commit()
    await network.pause_peer_reads(bacap_stream=stream)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None and not rcw.read_paused
    assert network.substream_progress_queue.empty()
