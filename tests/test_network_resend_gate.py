from __future__ import annotations

import asyncio
from typing import cast

import pytest

from katzenqt import network


@pytest.mark.asyncio
async def test_resend_loop_retries_when_the_connection_gate_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    async def gate(*, idle_retry_s: float = 0.0) -> bool:
        calls.append(idle_retry_s)
        if len(calls) > 1:
            network.shutdown()
        return False

    monkeypatch.setattr(network, "_wait_for_connection_or_shutdown", gate)
    await asyncio.wait_for(
        network.send_resendable_plaintexts(cast("object", None)), timeout=5,
    )
    assert len(calls) >= 2
    assert calls[0] == network._CONNECTION_IDLE_RETRY_S
