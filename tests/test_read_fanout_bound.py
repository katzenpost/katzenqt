from __future__ import annotations

import asyncio

import pytest

from katzenqt import network


@pytest.mark.asyncio
async def test_a_slot_is_shared_within_one_loop() -> None:
    assert network.read_fanout_slot() is network.read_fanout_slot()


@pytest.mark.asyncio
async def test_no_more_reads_run_at_once_than_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(network, "READ_FANOUT_LIMIT", 3)
    monkeypatch.setattr(network, "_read_fanout_slots", {})
    live = 0
    peak = 0

    async def fake_read(**_kwargs: object) -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1

    monkeypatch.setattr(network, "drain_mixwal_read_single", fake_read)
    await asyncio.gather(*(
        network.drain_mixwal_read_bounded(n=n) for n in range(40)
    ))
    assert peak <= 3
    assert live == 0


def test_each_loop_gets_its_own_slot() -> None:
    """An asyncio primitive belongs to the loop that first awaits it, and
    the async engine is driven from both the Qt loop and the io thread."""
    seen = []

    async def grab() -> None:
        seen.append(network.read_fanout_slot())

    asyncio.run(grab())
    asyncio.run(grab())
    assert seen[0] is not seen[1]
