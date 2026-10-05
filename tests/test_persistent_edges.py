from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlmodel import Session

from katzenqt import persistent


def test_id_field_builds_a_sequence_backed_primary_key() -> None:
    field = persistent.id_field("widget")
    assert field.primary_key is True
    assert field.sa_column_args[0].name == "widget_id_seq"
    assert "server_default" in field.sa_column_kwargs


@pytest.mark.asyncio
async def test_asession_awaits_the_close_when_cancelled() -> None:
    started = asyncio.Event()

    async def hold() -> None:
        async with persistent.asession():
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.ensure_future(hold())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_wait_for_sent_returns_false_when_the_deadline_has_passed() -> (
    None
):
    assert (
        await persistent.wait_for_sent(
            uuid.uuid4(),
            deadline_s=-1.0,
        )
        is False
    )


@pytest.mark.asyncio
async def test_wait_for_sent_returns_true_once_the_row_is_there() -> None:
    pwal_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(persistent.SentLog(id=pwal_id))
        await sess.commit()
    assert (
        await persistent.wait_for_sent(
            pwal_id,
            deadline_s=5.0,
            poll_s=0.01,
        )
        is True
    )


def _mixwal(stream: uuid.UUID, pwal_id: uuid.UUID) -> persistent.MixWAL:
    return persistent.MixWAL(
        id=uuid.uuid4(),
        plaintextwal=pwal_id,
        bacap_stream=stream,
        envelope_hash=uuid.uuid4().bytes * 2,
        encrypted_payload=b"payload",
        envelope_descriptor=b"descriptor",
        current_message_index=b"\x00" * 104,
        next_message_index=b"\x01" * 104,
        is_read=False,
    )


def test_mark_sent_txn_reaps_the_mixwal_when_the_pwal_is_gone() -> None:
    stream = uuid.uuid4()
    missing_pwal = uuid.uuid4()
    mw = _mixwal(stream, missing_pwal)
    with Session(persistent._engine_sync) as sess:
        sess.add(mw)
        sess.commit()
        mw_id = mw.id

    result = persistent._mark_sent_txn(
        mw_id,
        stream,
        missing_pwal,
        False,
        b"\x02" * 104,
        2,
        1,
    )
    assert result is None
    with Session(persistent._engine_sync) as sess:
        assert sess.get(persistent.MixWAL, mw_id) is None


@pytest.mark.asyncio
async def test_wait_for_sent_polls_until_the_row_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwal_id = uuid.uuid4()
    real_sleep = asyncio.sleep
    polls = 0

    async def insert_on_the_first_poll(delay: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 1:
            async with persistent.asession() as sess:
                sess.add(persistent.SentLog(id=pwal_id))
                await sess.commit()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", insert_on_the_first_poll)
    assert await persistent.wait_for_sent(
        pwal_id, deadline_s=5.0, poll_s=0.01,
    ) is True
    assert polls >= 1


def test_mark_sent_txn_logs_a_missing_write_cap_and_a_stream_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pwal_stream = uuid.uuid4()
    other_stream = uuid.uuid4()
    pwal_id = uuid.uuid4()
    pwal = persistent.PlaintextWAL(
        id=pwal_id,
        bacap_stream=pwal_stream,
        conversation_id=1,
        bacap_payload=b"body",
        indirection=uuid.uuid4(),
    )
    mw = _mixwal(other_stream, pwal_id)
    with Session(persistent._engine_sync) as sess:
        sess.add(pwal)
        sess.add(mw)
        sess.commit()
        mw_id = mw.id

    with caplog.at_level("ERROR"):
        persistent._mark_sent_txn(
            mw_id,
            other_stream,
            pwal_id,
            False,
            b"\x02" * 104,
            2,
            1,
        )
    assert "no WriteCapWAL for bacap_stream" in caplog.text
    assert "doesn't match pwal.bacap_stream" in caplog.text


@pytest.mark.real_sleeps
@pytest.mark.asyncio
async def test_asession_finishes_the_close_when_cancelled_mid_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession

    closing = asyncio.Event()
    finished: list[bool] = []
    original = AsyncSession.close

    async def slow_close(self: AsyncSession) -> None:
        closing.set()
        await asyncio.sleep(0.2)
        await original(self)
        finished.append(True)

    monkeypatch.setattr(AsyncSession, "close", slow_close)

    async def body() -> None:
        async with persistent.asession():
            pass

    task = asyncio.ensure_future(body())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]


@pytest.mark.real_sleeps
@pytest.mark.asyncio
async def test_asession_finishes_the_acquire_when_cancelled_mid_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession

    acquiring = asyncio.Event()
    original = AsyncSession.connection

    async def slow_connection(self: AsyncSession) -> object:
        acquiring.set()
        await asyncio.sleep(0.2)
        return await original(self)

    monkeypatch.setattr(AsyncSession, "connection", slow_connection)

    async def body() -> None:
        async with persistent.asession():
            pass

    task = asyncio.ensure_future(body())
    await acquiring.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_read_cap_row_is_fetched_by_its_stream() -> None:
    stream = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.ReadCapWAL(
                id=stream,
                write_cap_id=stream,
                next_index=b"\x00" * 104,
            )
        )
        await sess.commit()
    async with persistent.asession() as sess:
        row = await persistent.ReadCapWAL.get_by_bacap_stream(sess, stream)
        assert row.id == stream
