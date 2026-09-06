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
import threading
import uuid

import pytest
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
    """The actual send-path writer, not a hand-rolled copy of its
    lock/count-subquery/commit sequence: a bug specific to
    append_outbound_chat's own argument handling or lock/session ordering
    should be caught here."""
    await persistent.append_outbound_chat(
        conversation_id=conversation_id,
        conversation_peer_id=peer_id,
        new_write_caps=[],
        db_entries=[],
        payload=payload,
    )


async def _orders(conversation_id: int) -> list[int]:
    async with persistent.asession() as sess:
        rows = (await sess.exec(
            select(persistent.ConversationLog.conversation_order)
            .where(
                persistent.ConversationLog.conversation_id == conversation_id
            )
        )).all()
    return sorted(rows)


@pytest.mark.real_sleeps
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


def test_lock_blocks_a_genuinely_different_thread():
    # The single-loop test above only exercises the same-thread deadlock
    # this lock was fixed to avoid; it says nothing about the cross-thread
    # case (GUI loop vs. io loop) the lock's own docstring claims to
    # handle. Exercise that directly, on the lock alone (no DB/aiosqlite
    # involved, which has its own cross-loop hazards orthogonal to this
    # lock) by holding it from one real OS thread while a second contends
    # for the same conversation_id.
    conversation_id = 999999
    main_holds_it = threading.Event()
    other_acquired = threading.Event()
    other_thread_done = threading.Event()

    def other_thread_body():
        async def acquire_once():
            # Deterministic ordering: never even try until the main thread
            # has confirmed it holds the lock, so this is never a race.
            main_holds_it.wait(timeout=5)
            async with persistent.conversation_log_order_lock(conversation_id):
                other_acquired.set()
        asyncio.run(acquire_once())
        other_thread_done.set()

    async def hold_it():
        async with persistent.conversation_log_order_lock(conversation_id):
            main_holds_it.set()
            await asyncio.sleep(0.2)
            assert not other_acquired.is_set(), (
                "other thread acquired the lock while this thread still held it"
            )

    t = threading.Thread(target=other_thread_body)
    t.start()
    try:
        asyncio.run(hold_it())
    finally:
        t.join(timeout=5)
    assert other_thread_done.is_set(), "other thread never finished"
    assert other_acquired.is_set(), "other thread never acquired the lock at all"