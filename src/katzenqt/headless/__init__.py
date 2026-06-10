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

    Quiet by default: only each verb's result tokens (``CREATED``, ``VOUCHER=``,
    ``SENT``, ``RECV=``, ``TALLY=``, ..., logged on ``katzen.headless``) and any
    warnings/errors are shown. Set ``KQT_LOG_LEVEL`` (e.g. ``INFO`` or
    ``DEBUG``) for the full verbose stream.

    The SQLAlchemy statement echo is turned off on the engines in :func:`cli`,
    because it logs through a per-engine logger that ignores logging levels and
    so cannot be hushed here.
    """
    override = os.environ.get("KQT_LOG_LEVEL")
    if override:
        logging.basicConfig(
            stream=sys.stderr,
            level=getattr(logging, override.upper(), logging.INFO),
            format="%(levelname)s %(name)s: %(message)s",
            force=True,
        )
        return

    # Default: quiet. force=True so we own the root handler even though
    # importing persistent.py already touched logging.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # Several libraries (notably the thin client) force their own logger to
    # DEBUG, so their records reach the root handler regardless of the root
    # logger's level. Filter at the *handler* so only WARNING+ gets through.
    for h in logging.root.handlers:
        h.setLevel(logging.WARNING)
    # SQLAlchemy's async pool logs a benign "Exception during reset" at ERROR
    # when the loop tears down with connections a cancelled task abandoned.
    # That is teardown noise, not a query result; hush it (verbose mode, via
    # KQT_LOG_LEVEL, still shows it).
    logging.getLogger("sqlalchemy").setLevel(logging.CRITICAL)
    # Emit the verbs' result tokens bare (no level/name prefix) on their own
    # handler, and stop them propagating to the root handler, so the quiet
    # output is purely the query result.
    results = logging.getLogger("katzen.headless")
    results.setLevel(logging.INFO)
    results.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    results.addHandler(handler)


def cli(argv: "list[str] | None" = None) -> int:
    """Console-script entry point for the headless CLI.

    Brings the schema to head via :func:`persistent.init_and_migrate`
    (which itself calls ``asyncio.run`` and therefore must execute
    before we instantiate our own event loop), then dispatches the
    chosen action.
    """
    args = _actions._build_parser().parse_args(argv)
    if not os.environ.get("KQT_LOG_LEVEL"):
        # The engines are created with echo=True, which logs every SQL
        # statement through a per-engine logger that ignores logging levels.
        # For the quiet default, turn it off at the source (before
        # init_and_migrate, so its statements stay quiet too).
        persistent._engine.echo = False
        persistent._engine_sync.echo = False
    # After init_and_migrate: Alembic's env.py runs fileConfig() from
    # alembic.ini, which resets the root logger to WARNING. Configuring
    # afterwards lets our level stand.
    persistent.init_and_migrate()
    _configure_logging()

    # Connecting verbs carry the explicit `--config` / `--address`; resolve them
    # to a thinclient.toml path now and hand it to the actions. `info` has no
    # such arguments and needs no daemon.
    temp_config = None
    if hasattr(args, "config"):
        config_path, temp_config = _actions.resolve_connection_config(args)
        _actions.set_connection_config(config_path)

    loop = asyncio.new_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, loop.stop)
    except NotImplementedError:
        pass  # Windows or sandboxed envs
    try:
        return loop.run_until_complete(args.func(args))
    finally:
        # Cancel any stragglers (fire-and-forget read/send loops) and let them
        # unwind while the loop is still running, so we do not close it under an
        # in-flight thin-client call ("Event loop is closed" / "Task was
        # destroyed but it is pending").
        stragglers = asyncio.all_tasks(loop)
        for task in stragglers:
            task.cancel()
        if stragglers:
            loop.run_until_complete(asyncio.gather(*stragglers, return_exceptions=True))
        # Dispose the async engine's connection pool inside the running loop;
        # otherwise its aiosqlite connections are finalised as the loop closes
        # and their rollback fails ("Exception during reset", CancelledError).
        loop.run_until_complete(persistent._engine.dispose())
        loop.close()
        if temp_config is not None:
            try:
                os.remove(temp_config)
            except OSError:
                pass


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
