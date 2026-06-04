"""Run ``katzenqt`` without the Qt GUI.

This module is the supported entry point for local applications that
wish to send and receive messages and files using a ``katzenqt`` state
file. It is deliberately free of any ``PySide6`` import so headless
consumers, including pytest collection and the integration runner,
pay no Qt cost.

Two surfaces are exposed:

* a small async Python API (``connect``, ``start``, ``stop``,
  ``session``) for in-process callers,
* a line-oriented CLI driven by :func:`cli`, also installed as the
  ``katzenqt-headless`` console script.

Typical Python usage::

    import asyncio
    from katzenqt import headless

    async def main():
        async with headless.session() as client:
            ...  # drive the network through katzenqt.network's API

    asyncio.run(main())

For the CLI surface, run ``katzenqt-headless --help`` or
``python -m katzenqt.headless --help``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from katzenpost_thinclient import ThinClient

from . import _actions
from .. import network, persistent
from ..network import resolve_thinclient_config


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


async def stop(
    bg: asyncio.Task,
    connection: ThinClient,
    timeout: float = 5.0,
) -> None:
    """Signal the background worker to wind down, wait for it, and
    close the ``ThinClient``.

    On timeout the background task is cancelled. Cancellation and
    timeout are swallowed; callers that need to know the cause should
    inspect ``bg`` after ``stop`` returns.

    Calling :meth:`ThinClient.stop` is mandatory: it sends a
    ``thin_close`` message to kpclientd so the daemon reaps the ARQ
    state held against this connection. Without it, stale ARQ
    entries linger and crowd out new requests, which manifests as
    rapidly increasing latency across repeated subprocess invocations.
    """
    network.shutdown()
    try:
        await asyncio.wait_for(bg, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        bg.cancel()
    connection.stop()


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
        await stop(bg, connection)


def _configure_logging() -> None:
    """Send katzenqt's logs to stderr for headless runs.

    The headless CLI has no GUI to surface status, so diagnostics and
    results go through the logging framework. KQT_LOG_LEVEL overrides the
    default INFO level.
    """
    level = os.environ.get("KQT_LOG_LEVEL", "INFO").upper()
    # force=True so we own the root handler and level even though importing
    # persistent.py (engines created with echo=True) has already touched the
    # logging machinery; without it basicConfig would no-op and our INFO
    # records would be filtered by the default WARNING root level.
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


def cli(argv: "list[str] | None" = None) -> int:
    """Console-script entry point for the headless CLI.

    Brings the schema to head via :func:`persistent.init_and_migrate`
    (which itself calls ``asyncio.run`` and therefore must execute
    before we instantiate our own event loop), then dispatches the
    chosen action.
    """
    args = _actions._build_parser().parse_args(argv)
    # After init_and_migrate: Alembic's env.py runs fileConfig() from
    # alembic.ini, which resets the root logger to WARNING. Configuring
    # afterwards lets our level stand.
    persistent.init_and_migrate()
    _configure_logging()

    loop = asyncio.new_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, loop.stop)
    except NotImplementedError:
        pass  # Windows or sandboxed envs
    try:
        return loop.run_until_complete(args.func(args))
    finally:
        loop.close()


__all__ = [
    "cli",
    "connect",
    "resolve_thinclient_config",
    "session",
    "start",
    "stop",
]


if __name__ == "__main__":
    sys.exit(cli())
