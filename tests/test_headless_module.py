"""Public surface and life-cycle guards for ``katzenqt.headless``.

The Qt GUI's collaborator runs in another process. This module pins:

* the documented public symbols exist with their stated shapes,
* importing the module does not pull in PySide6,
* ``start`` and ``stop`` round-trip cleanly against a fake client,
* the async ``session`` context manager handles cleanup on exit and on
  exception.
"""
from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys

import pytest
import pytest_asyncio

from katzenqt import headless, network


def test_public_api_is_what_we_claim():
    """The plan promises five symbols. Each must be present and have
    the expected callable shape."""
    assert callable(headless.resolve_thinclient_config)
    assert headless.resolve_thinclient_config is network.resolve_thinclient_config

    assert inspect.iscoroutinefunction(headless.connect)
    sig = inspect.signature(headless.connect)
    assert "config_path" in sig.parameters
    assert sig.parameters["config_path"].default is None

    assert inspect.iscoroutinefunction(headless.start)
    assert "connection" in inspect.signature(headless.start).parameters

    assert inspect.iscoroutinefunction(headless.stop)
    stop_sig = inspect.signature(headless.stop)
    assert "bg" in stop_sig.parameters
    # ``connection`` is required so callers cannot forget to send
    # ``thin_close`` to the daemon. Without that, kpclientd retains
    # ARQ state for the dead connection and crowds out new requests.
    assert "connection" in stop_sig.parameters
    assert "timeout" in stop_sig.parameters

    # ``session`` is an async context-manager factory built with
    # ``@asynccontextmanager``, so the function itself is sync but
    # returns an async context manager.
    assert callable(headless.session)
    sess_sig = inspect.signature(headless.session)
    assert "config_path" in sess_sig.parameters


def test_import_katzenqt_headless_does_not_load_pyside6():
    """The headless module must remain importable without Qt."""
    code = (
        "import katzenqt.headless\n"
        "import sys\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('PySide6'))\n"
        "print('PYSIDE_LOADED', bool(loaded), loaded)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("PYSIDE_LOADED"):
            assert line.split()[1] == "False", line
            return
    raise AssertionError(f"sentinel not found in:\n{out}")


@pytest.mark.asyncio
async def test_start_then_stop_with_fake_client(fake_thinclient):
    """``start`` produces a live background task; ``stop`` shuts it
    down within the timeout and closes the ThinClient. The fake
    stands in for the real ``ThinClient`` so neither kpclientd nor
    the mixnet is required."""
    await network.on_connection_status({"is_connected": True, "err": None})
    task = await headless.start(fake_thinclient)
    assert isinstance(task, asyncio.Task)
    assert not task.done()
    assert fake_thinclient.stopped is False  # precondition

    await headless.stop(task, fake_thinclient, timeout=5.0)
    assert task.done()
    # The ThinClient must be stopped so the daemon receives the
    # thin_close message and frees this connection's ARQ state.
    # Without it, repeated subprocess invocations would each leak
    # ARQ entries on kpclientd until create-conv itself stalls.
    assert fake_thinclient.stopped is True


@pytest_asyncio.fixture
async def _patched_connect(monkeypatch, fake_thinclient):
    """Make ``headless.connect`` return the fake without touching the
    real ``network.reconnect`` (which would try to dial kpclientd)."""
    async def fake_connect(config_path=None):
        await network.on_connection_status({"is_connected": True, "err": None})
        return fake_thinclient
    monkeypatch.setattr(headless, "connect", fake_connect)
    return fake_thinclient


@pytest.mark.asyncio
async def test_session_context_manager_round_trip(_patched_connect):
    """``async with headless.session()`` yields the connection,
    triggers the network shutdown signal on exit, and closes the
    ThinClient so kpclientd reaps its ARQ state."""
    fake = _patched_connect
    async with headless.session() as connection:
        assert connection is fake
        # The shutdown event must NOT be set while the body runs.
        assert not getattr(network, "__should_quit").is_set()
        assert fake.stopped is False

    # On exit the event must have been set and the ThinClient must
    # have been closed.
    assert getattr(network, "__should_quit").is_set()
    assert fake.stopped is True


@pytest.mark.asyncio
async def test_session_cleans_up_when_body_raises(_patched_connect):
    """An exception inside the ``async with`` body must still trigger
    the same cleanup as a clean exit (including the ThinClient close)."""
    fake = _patched_connect
    with pytest.raises(RuntimeError, match="boom"):
        async with headless.session():
            assert not getattr(network, "__should_quit").is_set()
            assert fake.stopped is False
            raise RuntimeError("boom")

    assert getattr(network, "__should_quit").is_set()
    assert fake.stopped is True
