import asyncio
import uuid
from types import SimpleNamespace

import pytest

from katzenqt import network

pytestmark = pytest.mark.asyncio


async def test_a_failing_worker_is_paced_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quit_event = asyncio.Event()
    slept: list[float] = []
    calls: list[int] = []

    async def flaky(connection: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("deterministic failure")
        quit_event.set()

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    async def immediate(*, idle_retry_s: float = 0.0) -> bool:
        return True

    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown", immediate,
    )
    await network._supervised(flaky, object())
    assert len(calls) == 2
    assert slept and slept[0] >= network._SUPERVISOR_RETRY_S


async def test_an_early_clean_return_restarts_rather_than_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quit_event = asyncio.Event()
    calls: list[int] = []

    async def returns_early(connection: object) -> None:
        calls.append(1)
        if len(calls) >= 3:
            quit_event.set()

    async def fake_sleep(delay: float) -> None:
        return None

    async def immediate(*, idle_retry_s: float = 0.0) -> bool:
        return True

    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown", immediate,
    )
    await network._supervised(returns_early, object())
    assert len(calls) == 3


async def test_drain_mixwal_no_longer_swallows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(connection: object) -> None:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(network, "drain_mixwal2", boom)
    with pytest.raises(RuntimeError, match="drain failed"):
        await network.drain_mixwal(object())


def test_failure_reason_drops_peer_chosen_text() -> None:
    hostile = ValueError("A" * 100000 + "\x00\x1b[31m")
    reason = network._failure_reason(hostile)
    assert reason == "ValueError"
    assert len(reason) < 64
    assert reason.isprintable()


async def test_dismissing_an_already_cleared_transfer_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katzenqt import persistent

    class Sess:
        async def __aenter__(self) -> "Sess":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, model: object, key: object) -> object:
            return SimpleNamespace(substream_failure=None)

    monkeypatch.setattr(persistent, "asession", Sess)
    await network.dismiss_failed_transfer(bacap_stream=uuid.uuid4())


async def test_a_slow_but_cancellable_join_still_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers a join that responds to cancellation.

    It does NOT cover a join stuck forever: the real _cancel_and_join shields
    its gather and swallows cancellation until its children finish, so
    wait_for cannot bound it and connection.stop() would never run. Bounding
    it conflicts with test_repeated_cancellation_waits_for_cleanup, which
    requires the client stay open until cleanup ends, so the limitation
    stands rather than being papered over here.
    """
    from katzenqt.headless import _actions

    stopped: list[bool] = []

    async def never_finishes(tasks: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(network, "shutdown", lambda: None)
    monkeypatch.setattr(network, "_cancel_and_join", never_finishes)
    bg = asyncio.create_task(asyncio.sleep(0))
    await bg
    client = SimpleNamespace(stop=lambda: stopped.append(True))
    await _actions._shutdown(bg, client, timeout=0.01)
    assert stopped == [True]



async def test_a_join_that_ignores_cancellation_still_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from katzenqt.headless import _actions

    stopped: list[bool] = []
    release = asyncio.Event()

    async def stubborn(tasks: object) -> None:
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(network, "shutdown", lambda: None)
    monkeypatch.setattr(network, "_cancel_and_join", stubborn)
    bg = asyncio.create_task(asyncio.sleep(0))
    await bg
    client = SimpleNamespace(stop=lambda: stopped.append(True))
    await _actions._shutdown(bg, client, timeout=0.01)
    assert stopped == [True], "the deadline must not be able to skip stop()"
    release.set()
    await asyncio.sleep(0)


async def test_backoff_resets_after_a_healthy_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quit_event = asyncio.Event()
    slept: list[float] = []
    calls: list[int] = []
    clock = {"t": 0.0}

    async def worker(connection: object) -> None:
        calls.append(1)
        if len(calls) in (1, 2):
            raise RuntimeError("early crash")
        if len(calls) == 3:
            clock["t"] += 3600.0
            raise RuntimeError("crash after a long healthy run")
        quit_event.set()

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    async def immediate(*, idle_retry_s: float = 0.0) -> bool:
        return True

    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(network.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown", immediate,
    )
    await network._supervised(worker, object())
    assert slept[1] > slept[0], "consecutive failures must back off"
    assert slept[2] == network._SUPERVISOR_RETRY_S, (
        "a long healthy run must reset the delay, not keep the ratchet"
    )
