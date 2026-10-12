from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from katzenqt import network
from katzenqt.headless import _actions

from tests.test_headless_actions_common import StubConnection, make_conversation, run_action

ADDRESS = ["--address", "127.0.0.1:64331"]


@pytest.fixture(autouse=True)
def _stubbed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        _actions, "_connect_and_start", AsyncMock(return_value=(StubConnection(), object())),
    )
    monkeypatch.setattr(_actions, "_shutdown", AsyncMock())


@pytest.mark.asyncio
async def test_scan_reports_what_it_did(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("room", peers=("bob",))
    scan = AsyncMock(return_value=network.ScanResult(1, True, 2))
    monkeypatch.setattr(network, "scan_member", scan)
    assert await run_action(["scan", "room", "bob", *ADDRESS]) == 0
    assert scan.await_args is not None
    assert scan.await_args.kwargs == {
        "conversation_id": handle.conversation_id, "peer_id": handle.peer_ids["bob"],
    }
    assert 'SCANNED={"rechecked": 1, "walked": true, "unread": 2}' in caplog.text


@pytest.mark.asyncio
async def test_scan_says_when_there_is_nothing_to_scan(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("room", peers=("bob",))
    monkeypatch.setattr(network, "scan_member", AsyncMock(return_value=None))
    assert await run_action(["scan", "room", "bob", *ADDRESS]) == 1
    assert "nothing to scan" in caplog.text


@pytest.mark.asyncio
async def test_scan_needs_the_conversation(caplog: pytest.LogCaptureFixture) -> None:
    assert await run_action(["scan", "room", "bob", *ADDRESS]) == 2
    assert "not found" in caplog.text


@pytest.mark.asyncio
async def test_scan_needs_the_member(caplog: pytest.LogCaptureFixture) -> None:
    await make_conversation("room", peers=("bob",))
    assert await run_action(["scan", "room", "me", *ADDRESS]) == 2
    assert "member 'me' not found" in caplog.text
