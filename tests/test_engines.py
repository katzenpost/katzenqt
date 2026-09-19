"""Engine-level sqlite pragmas: WAL journaling and a busy timeout.

These keep concurrent writers (the GUI send thread and the io receive
loop) from failing instantly with ``database is locked``, which used to
crash a drain task mid-commit and strand its stream.
"""
import pytest
from sqlmodel import select

from katzenqt import persistent


@pytest.mark.asyncio
async def test_warm_async_engine_opens_a_connection_on_this_loop():
    """The GUI calls this on the io loop before the Qt loop touches the async
    engine, so the pool's run-once connect mutex binds to the io loop instead
    of racing. Smoke-test that it completes and leaves the engine usable."""
    await persistent.warm_async_engine()
    async with persistent.asession() as sess:
        rows = (await sess.exec(select(persistent.Conversation))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_engines_enable_wal_and_busy_timeout():
    with persistent._engine_sync.connect() as conn:
        wal, = conn.exec_driver_sql("PRAGMA journal_mode").first()
        busy, = conn.exec_driver_sql("PRAGMA busy_timeout").first()
    assert wal == "wal"
    # Small on purpose: the sync engine is awaited on the event-loop thread
    # (mark_sent) and used from the Qt thread, so a large busy_timeout would
    # freeze those threads for its full duration on contention. 250ms absorbs
    # micro-contention and bounds any stall to a couple of timer ticks.
    assert busy == 250

    async with persistent._engine.connect() as conn:
        wal, = (await conn.exec_driver_sql("PRAGMA journal_mode")).first()
        busy, = (await conn.exec_driver_sql("PRAGMA busy_timeout")).first()
    assert wal == "wal"
    assert busy == 250