from uuid import UUID

import pytest
from sqlmodel import select

from katzenqt import network, persistent


@pytest.mark.parametrize("terminal, elapsed, fails", [
    (False, 0.0, False), (False, 29.0, False),
    (False, 30.0, True), (True, 0.0, True),
])
def test_missing_box_uses_one_bounded_retry_window(
    terminal: bool, elapsed: float, fails: bool,
) -> None:
    started, reason = network._substream_miss_state(
        10.0, terminal=terminal, now_s=10.0 + elapsed, budget_s=30.0,
    )
    assert started == 10.0
    assert (reason is not None) is fails


@pytest.mark.asyncio
async def test_missing_box_keeps_the_cursor_until_the_budget_expires() -> None:
    stream = UUID(int=100)
    cursor = b"i" * 104
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(
            id=stream, read_cap=b"r" * 136, next_index=cursor,
        ))
        sess.add(persistent.ConversationPeer(
            name=":substream:2:test", read_cap_id=stream,
        ))
        sess.add(persistent.MixWAL(
            bacap_stream=stream, envelope_hash=b"h", encrypted_payload=b"p",
            envelope_descriptor=b"d", current_message_index=cursor,
            next_message_index=b"j" * 104, is_read=True,
        ))
        await sess.commit()
    assert not await network._record_substream_miss(
        stream, terminal=False, now_s=10.0, budget_s=30.0,
    )
    assert network.substream_progress_queue.empty()
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None
        assert rcw.next_index == cursor
        assert rcw.substream_missing_since == 10.0
        assert rcw.substream_failure is None
        assert (await sess.exec(select(persistent.MixWAL))).first() is not None
    assert await network._record_substream_miss(
        stream, terminal=False, now_s=40.0, budget_s=30.0,
    )
    event = network.substream_progress_queue.get_nowait()
    assert event[:2] == ("failed", stream)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None
        assert rcw.next_index == cursor
        assert rcw.substream_failure == event[2]
        assert (await sess.exec(select(persistent.MixWAL))).first() is None
        peer = (await sess.exec(select(persistent.ConversationPeer))).one()
        assert not peer.active
    await network.resume_peer_reads(bacap_stream=stream)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None
        assert rcw.substream_failure is None
        assert rcw.substream_missing_since is None
        assert rcw.next_index == cursor


@pytest.mark.asyncio
async def test_failed_commit_does_not_publish_a_transfer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = UUID(int=101)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=stream))
        sess.add(persistent.ConversationPeer(
            name=":substream:2:test", read_cap_id=stream,
        ))
        await sess.commit()

    async def failed_commit(session: persistent.AsyncSession) -> None:
        raise RuntimeError("commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(persistent.AsyncSession, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            await network._record_substream_miss(
                stream, terminal=True, now_s=10.0, budget_s=30.0,
            )
    assert network.substream_progress_queue.empty()
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None and rcw.substream_failure is None
        peer = (await sess.exec(select(persistent.ConversationPeer))).one()
        assert peer.active
