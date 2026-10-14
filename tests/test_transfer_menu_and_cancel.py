import asyncio
import uuid

import pytest
from sqlmodel import col, select

from katzenqt import network, persistent
from tests.test_membership_hash import _make_conversation


def _write_mixwal(stream: uuid.UUID, *, plaintextwal: uuid.UUID | None = None,
                  is_read: bool = False) -> persistent.MixWAL:
    cursor = b"\x00" * 104
    return persistent.MixWAL(
        bacap_stream=stream, envelope_hash=uuid.uuid4().bytes,
        encrypted_payload=b"payload", envelope_descriptor=b"descriptor",
        current_message_index=cursor, next_message_index=b"\x01" * 104,
        is_read=is_read, plaintextwal=plaintextwal,
    )


async def _seed_upload(
    conv_id: int, agg: uuid.UUID, rcw_id: uuid.UUID,
) -> None:
    async with persistent.asession() as sess:
        sess.add(persistent.WriteCapWAL(
            id=agg, write_cap=b"\x02" * 168, next_index=b"\x00" * 104,
        ))
        sess.add(persistent.ReadCapWAL(
            id=rcw_id, write_cap_id=agg, substream_total_chunks=2,
            read_cap=b"\x03" * 136,
        ))
        await sess.commit()


@pytest.mark.asyncio
async def test_pause_upload_keeps_the_pending_write_row() -> None:
    """Pausing leaves the MixWAL and PlaintextWAL rows for an idempotent
    re-send on resume, and marks the WriteCapWAL paused."""
    conv_id = await _make_conversation()
    agg, rcw_id, pwal_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_upload(conv_id, agg, rcw_id)
    async with persistent.asession() as sess:
        sess.add(persistent.PlaintextWAL(
            id=pwal_id, bacap_stream=agg, conversation_id=conv_id,
            bacap_payload=b"Cchunk",
        ))
        sess.add(_write_mixwal(agg, plaintextwal=pwal_id))
        await sess.commit()

    await network.pause_upload(rcw_id=rcw_id)

    async with persistent.asession() as sess:
        wcw = await sess.get(persistent.WriteCapWAL, agg)
        assert wcw is not None and wcw.paused
        assert (await sess.exec(select(persistent.MixWAL).where(
            persistent.MixWAL.bacap_stream == agg,
        ))).first() is not None
        assert (await sess.exec(select(persistent.PlaintextWAL).where(
            persistent.PlaintextWAL.id == pwal_id,
        ))).first() is not None


@pytest.mark.asyncio
async def test_cancel_upload_removes_the_i_chunk_mixwal_row() -> None:
    """The I-chunk lives on the main stream, so its MixWAL row must be deleted
    by PlaintextWAL id, not by the agg stream."""
    conv_id = await _make_conversation()
    agg, rcw_id = uuid.uuid4(), uuid.uuid4()
    i_chunk_id, main_stream, chunk_id = (
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
    )
    await _seed_upload(conv_id, agg, rcw_id)
    async with persistent.asession() as sess:
        sess.add(persistent.PlaintextWAL(
            id=i_chunk_id, bacap_stream=main_stream, conversation_id=conv_id,
            bacap_payload=b"Iindirection", indirection=rcw_id,
        ))
        sess.add(persistent.PlaintextWAL(
            id=chunk_id, bacap_stream=agg, conversation_id=conv_id,
            bacap_payload=b"Cchunk",
        ))
        sess.add(_write_mixwal(agg))
        sess.add(_write_mixwal(main_stream, plaintextwal=i_chunk_id))
        await sess.commit()

    await network.cancel_upload(rcw_id=rcw_id)

    async with persistent.asession() as sess:
        assert (await sess.exec(select(persistent.MixWAL))).all() == []
        assert (await sess.exec(select(persistent.PlaintextWAL).where(
            col(persistent.PlaintextWAL.bacap_stream).in_((agg, main_stream)),
        ))).all() == []
        assert await sess.get(persistent.ReadCapWAL, rcw_id) is None
        assert await sess.get(persistent.WriteCapWAL, agg) is None


@pytest.mark.asyncio
async def test_pause_upload_cancels_the_in_flight_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agg = uuid.uuid4()
    started = asyncio.Event()

    async def in_flight() -> None:
        started.set()
        await asyncio.Event().wait()

    class Sess:
        async def __aenter__(self) -> "Sess":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, model: object, key: object) -> None:
            return None

        async def exec(self, query: object) -> "Sess":
            return self

        def all(self) -> list[object]:
            return []

        async def commit(self) -> None:
            return None

    async def stream_for(rcw_id: object) -> object:
        return agg

    monkeypatch.setattr(network.persistent, "asession", Sess)
    monkeypatch.setattr(network, "_upload_stream_for_rcw", stream_for)
    task = asyncio.create_task(in_flight())
    await started.wait()
    network._inflight_writes[agg] = task
    try:
        await network.pause_upload(rcw_id=agg)
        assert task.cancelled() or task.done()
    finally:
        task.cancel()
        network._inflight_writes.pop(agg, None)


@pytest.mark.asyncio
async def test_the_write_dispatch_registers_the_task_for_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = uuid.uuid4()
    started = asyncio.Event()

    async def blocking_write(
        connection: object, mw: persistent.MixWAL,
        draining: set[uuid.UUID],
    ) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(network, "drain_mixwal_write_single", blocking_write)
    async with persistent.asession() as sess:
        sess.add(persistent.WriteCapWAL(
            id=stream, write_cap=b"\x02" * 168, next_index=b"\x00" * 104,
        ))
        sess.add(_write_mixwal(stream))
        await sess.commit()
    getattr(network, "__resend_queue_populated").set()
    getattr(network, "__mixwal_updated").set()
    getattr(network, "__mixnet_connected").set()
    drain = network.drain_mixwal2(object())  # type: ignore[arg-type]
    loop_task = asyncio.create_task(drain)
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)
        assert list(network._inflight_writes) == [stream]
        assert isinstance(network._inflight_writes[stream], asyncio.Task)
    finally:
        network.shutdown()
        try:
            await asyncio.wait_for(loop_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
