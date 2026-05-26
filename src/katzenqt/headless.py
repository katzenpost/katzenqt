"""Run ``katzenqt`` without the Qt GUI.

This module is the supported entry point for local applications that
wish to send and receive messages and files using a ``katzenqt`` state
file. It is deliberately free of any ``PySide6`` import so headless
consumers, including pytest collection and the integration runner,
pay no Qt cost.

Typical usage::

    import asyncio
    from katzenqt import headless

    async def main():
        async with headless.session() as client:
            ...  # drive the network through katzenqt.network's API

    asyncio.run(main())

The lower-level helpers ``connect``, ``start``, and ``stop`` are
exposed for callers that need finer control over the life cycle of
the background task.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from katzenpost_thinclient import ThinClient

from . import network
from .network import resolve_thinclient_config


async def connect(config_path: "str | Path | None" = None) -> ThinClient:
    """Bring up a ``ThinClient`` connection.

    ``config_path`` follows the precedence chain documented on
    :func:`katzenqt.network.resolve_thinclient_config`. Returns once
    the underlying ``ThinClient.start`` has completed.
    """
    return await network.reconnect(config_path)


async def start(connection: ThinClient) -> asyncio.Task:
    """Spawn the background worker task for ``connection``.

    The returned task runs ``network.start_background_threads`` until
    :func:`stop` is called. The caller is responsible for awaiting
    the task (via :func:`stop` or directly) before exiting the
    process.
    """
    return asyncio.create_task(network.start_background_threads(connection))


async def stop(bg: asyncio.Task, timeout: float = 5.0) -> None:
    """Signal the background worker to wind down and wait for it.

    On timeout the task is cancelled. Cancellation and timeout are
    swallowed; callers that need to know the cause should inspect
    ``bg`` after ``stop`` returns.
    """
    network.shutdown()
    try:
        await asyncio.wait_for(bg, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        bg.cancel()


@asynccontextmanager
async def session(
    config_path: "str | Path | None" = None,
) -> "AsyncIterator[ThinClient]":
    """Connect, start the worker, yield the client, then shut down.

    Use this when the life cycle of the worker matches the scope of
    a single ``async with`` block. If the body raises, the worker is
    still shut down via :func:`stop` before the exception propagates.
    """
    connection = await connect(config_path)
    bg = await start(connection)
    try:
        yield connection
    finally:
        await stop(bg)


__all__ = [
    "connect",
    "resolve_thinclient_config",
    "session",
    "start",
    "stop",
]
