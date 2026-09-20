"""Unit tests for katzen._commit_new_conversation.

The Qt-thread ``new_conversation`` coroutine (QInputDialog) is a manual test;
the risk lives in the DB write it delegates. This funnel — the io-loop writer
that replaced the ``_engine_sync``-on-the-GUI-thread commit — is tested
directly: the row set lands, and the same object instances carry the
generated ids/FKs back to the caller's ``convo`` for ``add_conversation``.
It is a one-shot write owned by ``new_conversation``; a stray repeat is a
no-op on the rows (re-added detached instances emit UPDATEs, not INSERTs).
"""
from __future__ import annotations

import uuid

import pytest

from katzenqt import katzen, persistent


def _build_objects(name="demo", own="me"):
    # Mirrors new_conversation's construction at katzen.py:new_conversation.
    wcapwal = persistent.WriteCapWAL(id=uuid.uuid4())
    rcapwal = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcapwal.id)
    convo = persistent.Conversation(name=name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name=own,
        read_cap_id=rcapwal.id,
        active=False,  # we are not reading from ourself
        conversation=convo,
    )
    convo.own_peer = own_peer
    first_post = persistent.ConversationLog(
        conversation=convo,
        conversation_peer=own_peer,
        conversation_order=0,
        payload=b"Your name in this conversation is " + own_peer.name.encode(),
    )
    return wcapwal, rcapwal, convo, own_peer, first_post


class TestCommitNewConversation:
    @pytest.mark.asyncio
    async def test_rows_land_and_objects_are_refreshed_in_place(self):
        wcapwal, rcapwal, convo, own_peer, first_post = _build_objects()
        assert convo.id is None and own_peer.id is None

        await katzen._commit_new_conversation(
            wcapwal, rcapwal, convo, own_peer, first_post
        )

        # The SAME instances must now carry the generated ids/FKs:
        assert convo.id is not None
        assert own_peer.id is not None
        assert convo.own_peer_id == own_peer.id
        assert convo.own_peer is own_peer
        assert own_peer.conversation is convo
        assert first_post.id is not None
        assert first_post.conversation_id == convo.id
        assert first_post.conversation_peer_id == own_peer.id
        assert first_post.conversation_order == 0
        assert wcapwal.id is not None and rcapwal.id is not None

        async with persistent.asession() as sess:
            convos = (await sess.exec(persistent.select(persistent.Conversation))).all()
            assert [c.name for c in convos] == ["demo"]
            assert convos[0].own_peer_id == own_peer.id
            peers = (await sess.exec(
                persistent.select(persistent.ConversationPeer)
            )).all()
            assert [p.name for p in peers] == ["me"]
            assert peers[0].active is False
            assert peers[0].read_cap_id == rcapwal.id
            logs = (await sess.exec(
                persistent.select(persistent.ConversationLog)
            )).all()
            assert len(logs) == 1
            assert logs[0].payload.startswith(b"Your name in this conversation is ")

    @pytest.mark.asyncio
    async def test_stray_second_call_does_not_widen_the_row_set(self):
        # The caller (new_conversation) guarantees the one-shot, so this is a
        # belly-flop fallback rather than a contract. Re-adding the (cleanish)
        # detached instances emits UPDATEs for the same PKs, so a stray repeat
        # is a no-op on the rows rather than the duplicate-then-IntegrityError
        # you might expect — the row set stays coherent either way.
        wcapwal, rcapwal, convo, own_peer, first_post = _build_objects()
        await katzen._commit_new_conversation(
            wcapwal, rcapwal, convo, own_peer, first_post
        )
        await katzen._commit_new_conversation(
            wcapwal, rcapwal, convo, own_peer, first_post
        )
        async with persistent.asession() as sess:
            convos = (await sess.exec(
                persistent.select(persistent.Conversation)
            )).all()
            logs = (await sess.exec(
                persistent.select(persistent.ConversationLog)
            )).all()
            peers = (await sess.exec(
                persistent.select(persistent.ConversationPeer)
            )).all()
            assert len(convos) == 1
            assert len(logs) == 1
            assert len(peers) == 1