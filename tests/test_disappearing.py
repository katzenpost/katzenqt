"""Disappearing messages: a conversation's policy deletes old content from
its log, and drops the Sent box's copy of what we sent."""

from __future__ import annotations

import asyncio
from typing import cast

import cbor2
import pytest
from sqlmodel import select

from katzenqt import acks, disappearing, models, network, persistent
from katzenqt.groupchat.sentbox import Retention
from tests.fakes.thinclient import FakeThinClient
from tests.test_acks import _Chat, _text, _with_bob

DAY = disappearing.DAY_SECONDS


async def _log(chat: _Chat, peer_id: int, payload: bytes, at: float, status: int = 0) -> None:
    async with persistent.asession() as sess:
        orders = await sess.exec(
            select(persistent.ConversationLog.conversation_order).where(
                persistent.ConversationLog.conversation_id == chat.conversation_id
            )
        )
        sess.add(persistent.ConversationLog(
            conversation_id=chat.conversation_id, conversation_peer_id=peer_id,
            payload=payload, conversation_order=max(orders, default=-1) + 1,
            logged_at=at, network_status=status,
        ))
        await sess.commit()


async def _payloads(chat: _Chat) -> list[bytes]:
    async with persistent.asession() as sess:
        rows = await sess.exec(
            select(persistent.ConversationLog.payload).where(
                persistent.ConversationLog.conversation_id == chat.conversation_id
            )
        )
        return list(rows)


def _f(gcm: models.GroupChatMessage) -> bytes:
    return b"F" + gcm.to_cbor()


INTRO = _f(models.GroupChatMessage(
    version=models.GROUP_CHAT_VERSION, membership_hash=bytes(32),
    introduction=models.GroupChatPleaseAdd(display_name="carol", read_cap=bytes(136)),
))


@pytest.mark.asyncio
async def test_old_content_disappears_and_the_rest_stays() -> None:
    chat, _ = await _with_bob()
    bob = chat.peers["bob"][0]
    await _log(chat, bob, _f(_text("old")), 0)
    await _log(chat, bob, _f(_text("new")), 2 * DAY)
    await _log(chat, bob, INTRO, 0)
    await _log(chat, chat.own_peer_id, _f(_text("mine, sent")), 0, status=2)
    await _log(chat, chat.own_peer_id, _f(_text("mine, acknowledged")), 0, status=3)
    await _log(chat, chat.own_peer_id, _f(_text("mine, unsent")), 0, status=1)
    await persistent.set_retention(chat.conversation_id, persistent.Retention(1, 1))
    assert await disappearing.expire(2 * DAY + 1) == [chat.conversation_id]
    assert sorted(await _payloads(chat)) == sorted(
        [_f(_text("new")), INTRO, _f(_text("mine, unsent"))]
    )
    assert await disappearing.expire(2 * DAY + 1) == []


@pytest.mark.asyncio
async def test_only_the_side_with_a_limit_disappears() -> None:
    chat, _ = await _with_bob()
    await _log(chat, chat.peers["bob"][0], _f(_text("theirs")), 0)
    await _log(chat, chat.own_peer_id, _f(_text("mine")), 0, status=2)
    await persistent.set_retention(chat.conversation_id, persistent.Retention(None, 1))
    await disappearing.expire(10 * DAY)
    assert await _payloads(chat) == [_f(_text("theirs"))]


@pytest.mark.asyncio
async def test_a_conversation_without_a_policy_keeps_everything() -> None:
    chat, _ = await _with_bob()
    await _log(chat, chat.peers["bob"][0], _f(_text("old")), 0)
    await persistent.set_retention(chat.conversation_id, persistent.Retention(3, None))
    await persistent.set_retention(chat.conversation_id, persistent.Retention())
    assert await disappearing.expire(100 * DAY) == []


@pytest.mark.asyncio
async def test_an_attachment_goes_with_the_last_message_naming_it() -> None:
    chat, _ = await _with_bob()
    files = persistent.state_file.parent / "attachments" / "1"
    files.mkdir(parents=True, exist_ok=True)
    (files / "a.bin").write_bytes(b"x")
    marker = b"F" + cbor2.dumps({"rel_path": "attachments/1/a.bin"})
    await _log(chat, chat.peers["bob"][0], marker, 0)
    await persistent.set_retention(chat.conversation_id, persistent.Retention(1, None))
    await disappearing.expire(2 * DAY)
    assert not (files / "a.bin").exists()


@pytest.mark.asyncio
async def test_the_sent_box_keeps_our_content_no_longer_than_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acks, "_replica_epoch", lambda: 10)
    chat, _ = await _with_bob()
    await persistent.set_retention(chat.conversation_id, persistent.Retention(None, 7))
    assert (await chat.group()).policy == Retention(keep_sent=2)
    (sent,) = await chat.queue(_text())
    index = chat.land(sent.id)
    await acks.tick()
    monkeypatch.setattr(acks, "_replica_epoch", lambda: 12)
    await acks.tick()
    (rewrite,) = await acks.rewrites()
    assert (rewrite.box_index, rewrite.payload) == (index, b"")


@pytest.mark.asyncio
async def test_the_upkeep_worker_tells_the_view_what_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _with_bob()
    await _log(chat, chat.peers["bob"][0], _f(_text("old")), 0)
    await persistent.set_retention(chat.conversation_id, persistent.Retention(1, None))
    worker = asyncio.create_task(network.upkeep(cast(network.ThinClient, FakeThinClient())))
    assert await network.conversation_update_queue.get() == (chat.conversation_id, True)
    getattr(network, "__should_quit").set()
    await worker
