import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from katzenqt import network

pytestmark = [pytest.mark.asyncio, pytest.mark.real_sleeps]


@dataclass
class _Clock:
    now: float = 0.0
    waits: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "_EPOCH_LOSS_STREAK", {})
    monkeypatch.setattr(network, "_reconnect_event", asyncio.Event())
    monkeypatch.setattr(network, "_epoch_event", asyncio.Event())


@pytest.mark.parametrize("signal", ["epoch", "reconnect"])
async def test_signal_retains_its_configured_grace(
    monkeypatch: pytest.MonkeyPatch, signal: str,
) -> None:
    clock = _Clock()
    marker = asyncio.Event()
    marker.set()

    async def wait(
        tasks: Iterable[asyncio.Future[object]], *,
        timeout: float, return_when: str,
    ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
        pending = set(tasks)
        clock.waits.append(timeout)
        if len(clock.waits) == 1:
            await asyncio.sleep(0)
            clock.now = 9.0
            winner = next(task for task in pending if task.done())
            return {winner}, pending - {winner}
        clock.now += timeout
        return set(), pending

    async def wait_for(
        task: asyncio.Future[object], *, timeout: float,
    ) -> object:
        clock.waits.append(timeout)
        clock.now += timeout
        task.cancel()
        raise TimeoutError

    monkeypatch.setattr(network, "asyncio", SimpleNamespace(
        ensure_future=asyncio.ensure_future, wait=wait, wait_for=wait_for,
        gather=asyncio.gather, FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
        TimeoutError=TimeoutError, get_running_loop=lambda: clock,
    ))
    with pytest.raises(network.ConnectionLifeInterruptedError):
        await network._rpc_racing_connection_life(
            bacap_uuid="stream", what="wait",
            rpc_factory=asyncio.Event().wait, backstop_s=10, grace_s=5,
            reconnect_marker=marker if signal == "reconnect" else None,
            epoch_marker=marker if signal == "epoch" else None,
        )
    assert clock.waits == [10, 5]


@pytest.mark.parametrize("signal", ["epoch", "reconnect"])
async def test_rpc_timeout_during_grace_is_not_a_watchdog_timeout(
    signal: str,
) -> None:
    marker = asyncio.Event()
    marker.set()
    failure = TimeoutError("RPC supplied this error")

    async def rpc() -> str:
        await asyncio.sleep(0.005)
        raise failure

    with pytest.raises(TimeoutError) as caught:
        await network._rpc_racing_connection_life(
            bacap_uuid="stream", what="wait", rpc_factory=rpc,
            backstop_s=1, grace_s=0.1,
            reconnect_marker=marker if signal == "reconnect" else None,
            epoch_marker=marker if signal == "epoch" else None,
        )
    assert caught.value is failure
    assert "stream" not in network._EPOCH_LOSS_STREAK


@pytest.mark.parametrize("signal", ["epoch", "reconnect", "backstop"])
async def test_interruption_reports_its_actual_reason(signal: str) -> None:
    marker = asyncio.Event()
    marker.set()
    with pytest.raises(network.ConnectionLifeInterruptedError) as caught:
        await network._rpc_racing_connection_life(
            bacap_uuid="stream", what="wait",
            rpc_factory=asyncio.Event().wait,
            backstop_s=0.05, grace_s=0.005,
            reconnect_marker=marker if signal == "reconnect" else None,
            epoch_marker=marker if signal == "epoch" else None,
        )
    assert caught.value.reason == signal
    assert caught.value.elapsed_s >= 0
    assert signal in str(caught.value)
