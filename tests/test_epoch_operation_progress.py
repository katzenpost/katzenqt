import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from katzenqt import network

pytestmark = [pytest.mark.asyncio, pytest.mark.real_sleeps]


@dataclass
class _Reader:
    reply: Callable[[], Awaitable[str]]

    async def start_resending_encrypted_message(
        self, **kwargs: object,
    ) -> str:
        return await self.reply()


@pytest.fixture(autouse=True)
def isolated_epoch_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "_EPOCH_LOSS_STREAK", {})
    monkeypatch.setattr(network, "_reconnect_event", asyncio.Event())
    monkeypatch.setattr(network, "_epoch_event", asyncio.Event())


async def _encrypted() -> str:
    return "encrypted"


async def test_setup_cannot_erase_repeated_delivery_losses() -> None:
    uid = "logical-read"
    epoch = asyncio.Event()
    epoch.set()
    for attempt in range(network._EPOCH_RACE_MAX_LOSSES):
        await network._rpc_racing_connection_life(
            bacap_uuid=uid, what="encrypt_read", rpc_factory=_encrypted,
        )
        with pytest.raises(network.ConnectionLifeInterruptedError):
            await network._rpc_racing_connection_life(
                bacap_uuid=uid, what="wait",
                rpc_factory=asyncio.Event().wait,
                epoch_marker=epoch, grace_s=0.005, backstop_s=1,
            )
        assert network._EPOCH_LOSS_STREAK[uid] == attempt + 1

    await network._rpc_racing_connection_life(
        bacap_uuid=uid, what="encrypt_read", rpc_factory=_encrypted,
    )

    async def delayed_reply() -> str:
        await asyncio.sleep(0.03)
        return "delivered"

    assert await network._await_read_reply(
        _Reader(delayed_reply), bacap_uuid=uid,
        read_watchdog_s=1, reconnect_grace_s=0.005, epoch_marker=epoch,
    ) == "delivered"
    assert uid not in network._EPOCH_LOSS_STREAK


async def test_failed_rpc_is_not_progress() -> None:
    network._EPOCH_LOSS_STREAK["stream"] = 2

    async def fail() -> str:
        raise ValueError("bad reply")

    with pytest.raises(ValueError, match="bad reply"):
        await network._rpc_racing_connection_life(
            bacap_uuid="stream", what="encrypt_read", rpc_factory=fail,
        )
    assert network._EPOCH_LOSS_STREAK["stream"] == 2


@pytest.mark.parametrize("signal", ["reconnect", "epoch"])
async def test_delivery_during_grace_resets_streak(signal: str) -> None:
    network._EPOCH_LOSS_STREAK["stream"] = 2
    marker = asyncio.Event()
    marker.set()

    async def reply() -> str:
        await asyncio.sleep(0.005)
        return "delivered"

    assert await network._await_read_reply(
        _Reader(reply), bacap_uuid="stream",
        read_watchdog_s=1, reconnect_grace_s=0.1,
        reconnect_marker=marker if signal == "reconnect" else None,
        epoch_marker=marker if signal == "epoch" else None,
    ) == "delivered"
    assert "stream" not in network._EPOCH_LOSS_STREAK
