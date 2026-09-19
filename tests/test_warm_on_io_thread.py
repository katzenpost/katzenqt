"""main() must still reach window.show() when the io thread has died.

AsyncioThread.run() dies on a startup error async_main does not retry (a stale
config, a missing socket path), and a hop to its dead loop never completes. The
warm-up hop has to give up on such a thread instead of waiting on it forever.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from katzenqt import katzen, network

_HANG_DEADLINE_S = 10


async def _finishes(coro) -> None:
    await asyncio.wait_for(coro, _HANG_DEADLINE_S)


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:coroutine 'warm_async_engine' was never awaited:RuntimeWarning"
)
async def test_a_dead_io_thread_does_not_block_startup(monkeypatch, caplog):
    async def reconnect_fails():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(network, "reconnect", reconnect_fails)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    thread = katzen.AsyncioThread(daemon=True)
    thread.start()

    with caplog.at_level(logging.ERROR, logger="katzen"):
        await _finishes(katzen._warm_async_engine_on_io_loop(thread))

    assert not thread.is_alive()
    assert "io thread exited before the async engine was warmed" in caplog.text


@pytest.mark.asyncio
async def test_a_live_io_thread_warms_the_engine(monkeypatch, caplog):
    async def reconnect_pending():
        await asyncio.Event().wait()

    monkeypatch.setattr(network, "reconnect", reconnect_pending)
    thread = katzen.AsyncioThread(daemon=True)
    thread.start()

    with caplog.at_level(logging.ERROR, logger="katzen"):
        await _finishes(katzen._warm_async_engine_on_io_loop(thread))

    assert thread.is_alive()
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_a_thread_dead_before_its_loop_exists_does_not_block_startup(caplog):
    thread = SimpleNamespace(is_alive=lambda: False)

    with caplog.at_level(logging.ERROR, logger="katzen"):
        await _finishes(katzen._warm_async_engine_on_io_loop(thread))

    assert "before its loop started" in caplog.text
