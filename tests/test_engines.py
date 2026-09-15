"""Engine-level sqlite pragmas: WAL journaling and a busy timeout.

These keep concurrent writers (the GUI send thread and the io receive
loop) from failing instantly with ``database is locked``, which used to
crash a drain task mid-commit and strand its stream.
"""
import pytest

from katzenqt import persistent


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