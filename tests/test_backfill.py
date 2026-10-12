"""The backfill worker writes each queued rewrite at its own box, through
the thin client, and leaves one that fails for the next sweep."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from katzenqt import acks, network
from tests.fakes.thinclient import FakeThinClient
from tests.test_acks import _text, _with_bob


async def _queued(monkeypatch: pytest.MonkeyPatch) -> acks.Rewrite:
    monkeypatch.setattr(acks, "_replica_epoch", lambda: 10)
    chat, _ = await _with_bob()
    (sent,) = await chat.queue(_text())
    chat.land(sent.id)
    await acks.tick()
    monkeypatch.setattr(acks, "_replica_epoch", lambda: 11)
    await acks.tick()
    (rewrite,) = await acks.rewrites()
    return rewrite


@pytest.mark.asyncio
async def test_a_rewrite_is_written_at_its_own_box(monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = await _queued(monkeypatch)
    client = FakeThinClient()
    await network._send_rewrite(cast(network.ThinClient, client), rewrite)
    assert client.box_store[rewrite.write_cap[32:], rewrite.box_index] == rewrite.payload
    assert await acks.rewrites() == []


@pytest.mark.asyncio
async def test_a_rewrite_that_fails_stays_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = await _queued(monkeypatch)
    client = FakeThinClient()
    client.inject_error("start_resending_encrypted_message", BrokenPipeError())
    await network._send_rewrite(cast(network.ThinClient, client), rewrite)
    assert await acks.rewrites() == [rewrite]


@pytest.mark.asyncio
async def test_a_newer_rewrite_of_the_box_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = await _queued(monkeypatch)
    await acks.rewritten(rewrite._replace(payload=b""))
    assert await acks.rewrites() == [rewrite]


@pytest.mark.asyncio
async def test_the_worker_sweeps_until_asked_to_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    rewrite = await _queued(monkeypatch)
    client = FakeThinClient()
    sweeps = 0
    tick = acks.tick

    async def counted() -> None:
        nonlocal sweeps
        sweeps += 1
        await tick()

    monkeypatch.setattr(acks, "tick", counted)
    monkeypatch.setattr(network, "_UPKEEP_SWEEP_S", 0.0)
    getattr(network, "__mixnet_connected").set()
    worker = asyncio.create_task(network.upkeep(cast(network.ThinClient, client)))
    while sweeps < 2:
        await asyncio.sleep(0.01)
    assert client.box_store[rewrite.write_cap[32:], rewrite.box_index] == rewrite.payload
    getattr(network, "__should_quit").set()
    await worker
