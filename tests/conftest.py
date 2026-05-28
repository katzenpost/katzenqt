"""Shared fixtures for katzenqt tests.

The parent `/home/human/katzenqt/conftest.py` has already pointed
`KQT_STATE` at a tmpdir before `persistent` was imported, so all sessions
share a disposable SQLite file. Fixtures in this module prepare schema and
wipe rows between tests, and stand a `FakeThinClient` plus a running
`start_background_threads` loop up for tests that drive `network.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

import katzenpost_thinclient
import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from katzenqt import network, persistent

from tests.fakes.thinclient import FakeThinClient


@pytest.fixture(autouse=True)
def _fresh_tables():
    """Drop + recreate all tables before every test.

    We skip alembic (it would try to read the repo's migrations/) and use
    sqlmodel's metadata directly, which is the source of truth the test
    subjects (MixWAL, PlaintextWAL, etc.) are actually defined against.
    """
    SQLModel.metadata.drop_all(persistent._engine_sync)
    SQLModel.metadata.create_all(persistent._engine_sync)
    yield


@pytest.fixture(autouse=True)
def _reset_network_module_state():
    """Re-create `network`'s module-level events fresh for each test and
    reset queues. asyncio.Event in 3.10+ binds to the loop on first
    `.wait()`, so an event used in a previous test's now-defunct loop
    cannot be reused. Wholesale replacement avoids the
    "Event is bound to a different event loop" surprise.
    """
    initial_set = (
        "__mixwal_updated",
        "readables_to_mixwal_event",
        "resendable_event",
    )
    initial_clear = (
        "__should_quit",
        "__mixnet_connected",
        "__resend_queue_populated",
    )

    def restore() -> None:
        for name in initial_set:
            ev = asyncio.Event()
            ev.set()
            setattr(network, name, ev)
        for name in initial_clear:
            setattr(network, name, asyncio.Event())
        getattr(network, "__resend_queue").clear()
        getattr(network, "__on_message_queues").clear()

    restore()
    yield
    restore()


@pytest.fixture(autouse=True)
def fast_asyncio_sleep(request, monkeypatch):
    """Replace `asyncio.sleep` with an instant `asyncio.sleep(0)` so the
    defensive long sleeps inside `network.drain_mixwal_read_single`
    (5s on decryption failure, 500s on courier-vanished) do not slow
    the suite. Tests that genuinely need scheduling can opt out with
    the `real_sleeps` marker.
    """
    if request.node.get_closest_marker("real_sleeps"):
        return
    real = asyncio.sleep

    async def instant(delay, result=None):
        return await real(0, result)

    monkeypatch.setattr(asyncio, "sleep", instant)


@pytest.fixture
def fake_thinclient(monkeypatch) -> FakeThinClient:
    """A fresh `FakeThinClient`. Also rebinds
    `katzenpost_thinclient.find_services` so `network.py`'s qualified
    calls reach the fake's controllable courier list rather than the
    real PKI traversal in `thin_client`."""
    fake = FakeThinClient()
    monkeypatch.setattr(
        katzenpost_thinclient, "find_services",
        lambda capability, doc: fake.find_services(capability),
    )
    return fake


@pytest_asyncio.fixture
async def live_network(fake_thinclient) -> AsyncIterator["LiveNetwork"]:
    """Run `network.start_background_threads(fake_thinclient)` inside an
    asyncio task while the test holds the fixture; cleanly shut it down
    on exit.

    The fixture pretends the mixnet has come online by firing
    ``on_connection_status({"is_connected": True})`` before starting the
    loops, so the `await __mixnet_connected.wait()` inside
    `start_background_threads` resolves immediately.
    """
    await network.on_connection_status({"is_connected": True, "err": None})
    task = asyncio.create_task(network.start_background_threads(fake_thinclient))

    handle = LiveNetwork(fake=fake_thinclient, task=task)
    try:
        # Give the background loops a chance to populate `__resend_queue`
        # from disk; tests that probe persistent state want that done.
        await asyncio.wait_for(
            getattr(network, "__resend_queue_populated").wait(), timeout=2.0,
        )
        yield handle
    finally:
        network.shutdown()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5.0)


class LiveNetwork:
    """Handle returned by the `live_network` fixture so tests can reach
    the fake and the background task without juggling tuples."""

    def __init__(self, *, fake: FakeThinClient, task: asyncio.Task) -> None:
        self.fake = fake
        self.task = task

    async def wait_for(self, predicate, *, timeout: float = 5.0) -> None:
        """Poll ``predicate()`` until it returns truthy or ``timeout``
        elapses. Use this in place of arbitrary sleeps when a test
        waits for a background loop to take effect."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            result = predicate() if not asyncio.iscoroutinefunction(predicate) else await predicate()
            if result:
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise AssertionError(f"predicate not satisfied within {timeout}s")
            await asyncio.sleep(0.02)
