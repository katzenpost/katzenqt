"""The last unexercised lines of the framing, entry point and stream teardown."""
from __future__ import annotations

import asyncio
import runpy
import sys
import uuid

import pytest

from katzenqt import models, network


def test_a_chain_that_never_ends_in_final_decodes_to_nothing() -> None:
    assert models.unserialize([(b"C", b"half a message")]) is None


def test_the_headless_module_runs_its_own_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["katzenqt-headless", "--help"])
    sys.modules.pop("katzenqt.headless", None)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("katzenqt.headless", run_name="__main__")
    assert caught.value.code == 0


@pytest.mark.asyncio
async def test_stopping_a_stream_passes_on_its_own_cancellation() -> None:
    stream = uuid.uuid4()
    started = asyncio.Event()

    async def never() -> None:
        started.set()
        await asyncio.Event().wait()

    inflight = asyncio.ensure_future(never())
    getattr(network, "_inflight_reads")[stream] = inflight
    await started.wait()

    stopping = asyncio.ensure_future(network.stop_stream(stream))
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert stopping.cancelled()

