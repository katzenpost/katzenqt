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


async def test_a_stuck_join_still_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
