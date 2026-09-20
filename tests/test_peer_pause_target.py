from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PySide6.QtGui import QStandardItem

from katzenqt import katzen, persistent


@pytest.mark.asyncio
async def test_peer_pause_target_is_scoped_and_unambiguous() -> None:
    ids: list[int] = []
    caps = [uuid4(), uuid4()]
    async with persistent.asession() as sess:
        for number, cap in enumerate(caps):
            rcw = persistent.ReadCapWAL(id=cap)
            peer = persistent.ConversationPeer(name="bob", read_cap_id=cap)
            conv = persistent.Conversation(name=f"room-{number}")
            conv.own_peer = peer
            conv.peers.append(peer)
            sess.add_all((rcw, peer, conv))
            await sess.commit()
            await sess.refresh(conv)
            ids.append(conv.id)
    with persistent.Session(persistent._engine_sync) as sess:
        peer = persistent.peer_named_in_conversation(sess, ids[1], "bob")
        assert peer is not None and peer.read_cap_id == caps[1]
    item = QStandardItem("room-1")
    window = SimpleNamespace(
        _wait_for_conversation_state=AsyncMock(return_value=True),
        conversation_state_by_id={ids[1]: SimpleNamespace(
            contacts_standard_item=item, own_peer_id=-1,
        )},
    )
    await katzen.MainWindow._process_peer_added(window, ids[1], "bob")
    assert item.child(0).peer_read_cap_id == caps[1]
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, ids[1])
        extra_cap = uuid4()
        sess.add(persistent.ReadCapWAL(id=extra_cap))
        sess.add(persistent.ConversationPeer(
            name="bob", read_cap_id=extra_cap, conversation=conv,
        ))
        await sess.commit()
    with persistent.Session(persistent._engine_sync) as sess:
        assert persistent.peer_named_in_conversation(sess, ids[1], "bob") is None
        first = persistent.peer_named_in_conversation(sess, ids[0], "bob")
        assert first is not None and first.read_cap_id == caps[0]
