from __future__ import annotations

import uuid

import pytest

from katzenqt import conversation_handlers, models, persistent

OWN_CAP = b"\x02" * 168
ALICE_CAP = b"\x01" * 136
EXTRA_CAP = b"\x03" * 136


async def _seed(extra_name: str | None, extra_active: bool) -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(),
        write_cap=OWN_CAP,
        next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        write_cap_id=wcapwal.id,
        read_cap=b"\x00" * 136,
        next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(
        name="demo",
        write_cap=wcapwal.id,
        first_unread=0,
    )
    own_peer = persistent.ConversationPeer(
        name="me",
        read_cap_id=rcapwal.id,
        active=True,
        conversation=convo,
    )
    convo.own_peer = own_peer
    alice_rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        read_cap=ALICE_CAP,
        next_index=b"\x01" * 104,
    )
    async with persistent.asession() as sess:
        for row in (wcapwal, rcapwal, convo, own_peer, alice_rcw):
            sess.add(row)
        sess.add(
            persistent.ConversationPeer(
                name="alice",
                read_cap_id=alice_rcw.id,
                active=True,
                conversation=convo,
            )
        )
        if extra_name is not None:
            extra_rcw = persistent.ReadCapWAL(
                id=uuid.uuid4(),
                read_cap=EXTRA_CAP,
                next_index=b"\x02" * 104,
            )
            sess.add(extra_rcw)
            sess.add(
                persistent.ConversationPeer(
                    name=extra_name,
                    read_cap_id=extra_rcw.id,
                    active=extra_active,
                    conversation=convo,
                )
            )
        await sess.commit()
        await sess.refresh(convo)
        return int(convo.id)


@pytest.mark.asyncio
async def test_already_has_is_false_when_the_cap_is_missing() -> None:
    conv_id = await _seed(None, True)
    intro = models.GroupChatPleaseAdd.model_construct(
        display_name="nobody",
        read_cap=None,
    )
    async with persistent.asession() as sess:
        assert (
            await conversation_handlers._already_has(
                sess,
                conv_id,
                intro,
            )
            is False
        )


@pytest.mark.asyncio
async def test_already_has_finds_an_existing_peer_cap() -> None:
    conv_id = await _seed(None, True)
    intro = models.GroupChatPleaseAdd(
        display_name="alice",
        read_cap=ALICE_CAP,
    )
    async with persistent.asession() as sess:
        assert (
            await conversation_handlers._already_has(
                sess,
                conv_id,
                intro,
            )
            is True
        )
