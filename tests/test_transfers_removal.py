"""The Transfers listener drops the row a TransferRemoved event names.

The removal arm is the one branch of ``transfers_listener`` no other test
reaches, so it is pinned here with its own model stub.
"""

from __future__ import annotations

import uuid

import pytest

from katzenqt import katzen, network
from tests.test_listener_hardening import (
    _fake_window,
    _FakeQueue,
    _run_until_cancelled,
)


class _RemovalModel:
    def __init__(self) -> None:
        self.removed: "list[uuid.UUID]" = []

    def remove_transfer(self, rcw_id: uuid.UUID) -> None:
        self.removed.append(rcw_id)


@pytest.mark.asyncio
async def test_a_removed_transfer_drops_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rcw = uuid.uuid4()
    queue = _FakeQueue([network.TransferRemoved(rcw)])
    monkeypatch.setattr(network, "substream_progress_queue", queue)
    model = _RemovalModel()
    window = _fake_window(transfers_model=model)

    await _run_until_cancelled(
        katzen.MainWindow.transfers_listener.__get__(window)()
    )

    assert model.removed == [rcw]
