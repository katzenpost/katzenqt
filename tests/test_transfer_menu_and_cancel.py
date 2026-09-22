import ast
import asyncio
import io
import inspect
import textwrap
import uuid

import pytest
from sqlmodel import select

from katzenqt import katzen, network, persistent
from tests.test_membership_hash import _make_conversation

pytestmark = pytest.mark.asyncio


def _write_mixwal(stream: uuid.UUID, *, plaintextwal: uuid.UUID | None = None,
                  is_read: bool = False) -> persistent.MixWAL:
    cursor = b"\x00" * 104
    return persistent.MixWAL(
        bacap_stream=stream, envelope_hash=uuid.uuid4().bytes,
        encrypted_payload=b"payload", envelope_descriptor=b"descriptor",
        current_message_index=cursor, next_message_index=b"\x01" * 104,
        is_read=is_read, plaintextwal=plaintextwal,
    )


async def _seed_upload(conv_id: int, agg: uuid.UUID, rcw_id: uuid.UUID):
    async with persistent.asession() as sess:
        sess.add(persistent.WriteCapWAL(
            id=agg, write_cap=b"\x02" * 168, next_index=b"\x00" * 104,
        ))
        sess.add(persistent.ReadCapWAL(
            id=rcw_id, write_cap_id=agg, substream_total_chunks=2,
            read_cap=b"\x03" * 136,
        ))
        await sess.commit()


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
            persistent.PlaintextWAL.bacap_stream.in_((agg, main_stream)),
        ))).all() == []
        assert await sess.get(persistent.ReadCapWAL, rcw_id) is None
        assert await sess.get(persistent.WriteCapWAL, agg) is None


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


def test_the_write_dispatch_registers_the_task_for_cancellation() -> None:
    src = inspect.getsource(network.drain_mixwal2)
    assert "_inflight_writes[" in src, (
        "pause_upload and cancel_upload can only cancel a write that the "
        "drain loop registered in _inflight_writes"
    )


def _menu_node() -> ast.AST:
    """The real method body. The attribute is decorated, so inspect.getsource
    returns the wrapper rather than the code under test."""
    path = inspect.getsourcefile(katzen)
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name == "transfers_context_menu"):
            return node
    raise AssertionError("transfers_context_menu not found")


def test_the_transfers_menu_does_not_await_a_sync_session() -> None:
    for node in ast.walk(_menu_node()):
        if isinstance(node, ast.With):
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    fn = getattr(child.value, "func", None)
                    name = getattr(fn, "attr", "")
                    assert name not in ("get", "exec"), (
                        "sync Session.%s is not awaitable" % name
                    )


def test_the_failed_row_branch_is_not_duplicated() -> None:
    removes = [
        n for n in ast.walk(_menu_node())
        if isinstance(n, ast.Constant) and n.value == "Remove"
    ]
    assert len(removes) == 1
