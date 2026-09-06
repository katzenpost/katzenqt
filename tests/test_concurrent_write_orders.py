"""Regression tests for the per-conversation log-order writer lock.

The appends that stamp ``ConversationLog.conversation_order`` run from two
different event loops (GUI outbound sends vs io-loop receives/vouchers), so
``persistent.conversation_log_order_lock`` must stay a real cross-thread
``threading.Lock``; but acquisition must never block the caller's event loop.
Before the fix, two coroutines on the same loop targeting the same
conversation froze the loop: the second blocked in ``Lock.acquire()`` while
the first was awaiting and could never resume. These tests hammer one
conversation from concurrent coroutines on a single loop and assert the
appends complete and stamp strictly unique orders.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func
from sqlmodel import select

from katzenqt import persistent


async def _make_conversation() -> tuple[int, int]:
    """Create one writable conversation; return (conversation_id, peer_id)."""
    stream = uuid.uuid4()
    async with persistent.asession() as sess:
        wcw = persistent.WriteCapWAL(id=stream)
        rcw = persistent.ReadCapWAL(id=stream, write_cap_id=stream)
        cpeer = persistent.ConversationPeer(
            name="peer",
            read_cap_id=stream,
            active=True,
        )
        sess.add_all([wcw, rcw, cpeer])
        await sess.commit()
        await sess.refresh(cpeer)
        conv = persistent.Conversation(
            name="conv",
            own_peer_id=cpeer.id,
            write_cap=stream,
        )
        sess.add(conv)
        await sess.commit()
        await sess.refresh(conv)
        return conv.id, cpeer.id


async def _append_log(conversation_id: int, peer_id: int, payload: bytes) -> None:
    """The send-path append pattern from katzen.py chat_msg_single_line."""
    async with persistent.conversation_log_order_lock(conversation_id):
        async with persistent.asession() as sess:
            sess.add(persistent.ConversationLog(
                conversation_id=conversation_id,
                conversation_peer_id=peer_id,
                conversation_order=(
                    select(func.count())
                    .select_from(persistent.ConversationLog)
                    .where(
                        persistent.ConversationLog.conversation_id
                        == conversation_id
                    )
                    .scalar_subquery()
                ),
                payload=payload,
                network_status=1,
            ))
            await sess.commit()


async def _orders(conversation_id: int) -> list[int]:
    async with persistent.asession() as sess:
        rows = (await sess.exec(
            select(persistent.ConversationLog.conversation_order)
            .where(
                persistent.ConversationLog.conversation_id == conversation_id
            )
        )).all()
    return sorted(rows)


@pytest.mark.asyncio
async def test_concurrent_appends_same_conversation_do_not_deadlock():
    conversation_id, peer_id = await _make_conversation()
    n = 8

    async def worker(i: int) -> None:
        await _append_log(conversation_id, peer_id, b"msg-%d" % i)

    results = await asyncio.wait_for(
        asyncio.gather(*(worker(i) for i in range(n)), return_exceptions=True),
        timeout=10,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent appends failed: {failures!r}"
    assert await _orders(conversation_id) == list(range(n))