"""A stored conversation for tests: ourselves and one peer, alice."""
from __future__ import annotations

import uuid

from katzenqt import persistent


async def make_conversation(name: str = "demo") -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x02" * 168, next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcapwal.id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(name=name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=rcapwal.id, active=True, conversation=convo,
    )
    convo.own_peer = own_peer
    peer_rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x01" * 136, next_index=b"\x01" * 104,
    )
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(peer_rcw)
        sess.add(persistent.ConversationPeer(
            name="alice", read_cap_id=peer_rcw.id, active=True, conversation=convo,
        ))
        await sess.commit()
        await sess.refresh(convo)
        return convo.id
