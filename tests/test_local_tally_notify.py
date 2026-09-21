"""A local tally create/vote/close writes a ConversationLog row and must wake
the chat view (conversation_update_queue), so the author's own row appears
without waiting for another message.

This is the writer that previously appended a log row silently, leaving the
GUI's row count (a cached count) behind until an unrelated message nudged it.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from katzenqt import katzen, network, persistent
from katzenqt.tally.schema import Mode


async def _make_convo(name: str = "g") -> int:
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcap.id, read_cap=bytes([0x01]) * 136,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=own_rcap.id, conversation=convo,
    )
    convo.own_peer = own_peer
    async with persistent.asession() as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        await sess.commit()
        await sess.refresh(convo)
        return convo.id


def _drain(queue) -> "list":
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@pytest.fixture(autouse=True)
def _reset_chat_queue():
    # A fresh queue keeps items from one test out of the next; replace the
    # module-level queue so the coroutine binds to this test's loop, and put
    # the original back afterwards so later test modules see the real queue.
    orig_queue = network.conversation_update_queue
    network.conversation_update_queue = asyncio.Queue()
    # check_for_new is unrelated to this assertion; stub it out.
    orig = network.check_for_new
    async def _noop():
        return None
    network.check_for_new = _noop
    yield
    network.conversation_update_queue = orig_queue
    network.check_for_new = orig


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["create", "vote", "close"])
async def test_local_tally_op_wakes_the_chat_view(op):
    convo_id = await _make_convo()
    if op == "create":
        survey_id = None
    else:
        # Seed a survey we created.
        survey_id = uuid.uuid4().bytes
        async with persistent.asession() as sess:
            convo = await sess.get(persistent.Conversation, convo_id)
            from katzenqt.tally.controller import INSTANCE
            await INSTANCE.create_local(sess, convo, survey_id, "t", Mode.APPROVAL, ["a"])
            await sess.commit()

    _drain(network.conversation_update_queue)
    if op == "create":
        await katzen._io_tally_create(convo_id, "topic", Mode.APPROVAL, ["a"])
    elif op == "vote":
        await katzen._io_tally_vote(convo_id, survey_id, {"s0": "yes"})
    else:
        await katzen._io_tally_close(convo_id, survey_id)

    assert _drain(network.conversation_update_queue) == [(convo_id, False)]
