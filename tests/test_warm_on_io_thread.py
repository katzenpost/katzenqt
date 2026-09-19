"""main() must still reach window.show() when the io thread has died.

The io thread warms the async engine before anything else, and the Qt loop
waits for that. AsyncioThread.run() dies on a startup error async_main does not
retry (a stale config, a missing socket path); the wait must not outlive it.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import threading
import warnings
from types import SimpleNamespace

import pytest

from katzenqt import katzen, network

_HANG_DEADLINE_S = 10


async def _finishes(coro) -> None:
    await asyncio.wait_for(coro, _HANG_DEADLINE_S)


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_the_engine_is_warmed_before_the_io_thread_connects(monkeypatch, caplog):
    seen = []

    async def reconnect_pending():
        seen.append(thread.engine_warmed.is_set())
        await asyncio.Event().wait()

    monkeypatch.setattr(network, "reconnect", reconnect_pending)
    thread = katzen.AsyncioThread(daemon=True)
    thread.start()

    with caplog.at_level(logging.ERROR, logger="katzen"):
        await _finishes(katzen._wait_for_engine_warmed(thread))
    await _finishes(_until(lambda: seen))

    assert seen == [True]
    assert thread.is_alive()
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_a_dead_io_thread_does_not_block_startup(monkeypatch, caplog):
    async def reconnect_fails():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(network, "reconnect", reconnect_fails)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        thread = katzen.AsyncioThread(daemon=True)
        thread.start()
        with caplog.at_level(logging.ERROR, logger="katzen"):
            await _finishes(katzen._wait_for_engine_warmed(thread))
        await asyncio.to_thread(thread.join, _HANG_DEADLINE_S)
        assert not thread.is_alive()
        del thread
        gc.collect()

    assert caplog.text == ""
    assert [w for w in caught if "never awaited" in str(w.message)] == []


@pytest.mark.asyncio
async def test_a_failing_warm_up_does_not_block_startup(monkeypatch):
    async def warm_fails():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(katzen.persistent, "warm_async_engine", warm_fails)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    thread = katzen.AsyncioThread(daemon=True)
    thread.start()

    await _finishes(katzen._wait_for_engine_warmed(thread))
    await asyncio.to_thread(thread.join, _HANG_DEADLINE_S)

    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_a_thread_dead_before_warming_does_not_block_startup(caplog):
    thread = SimpleNamespace(
        engine_warmed=threading.Event(), is_alive=lambda: False,
    )

    with caplog.at_level(logging.ERROR, logger="katzen"):
        await _finishes(katzen._wait_for_engine_warmed(thread))

    assert "exited before it warmed" in caplog.text
