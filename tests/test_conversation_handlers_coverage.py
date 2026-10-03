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


def _expected_without_extra() -> bytes:
    return bytes(models.canonical_membership_hash([OWN_CAP[32:], ALICE_CAP]))


@pytest.mark.asyncio
async def test_an_inactive_peer_is_left_out_of_the_hash() -> None:
    conv_id = await _seed("bob", False)
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        got = await conversation_handlers.local_membership_hash(sess, convo)
    assert got == _expected_without_extra()


@pytest.mark.asyncio
async def test_a_substream_peer_is_left_out_of_the_hash() -> None:
    name = f"{models.SUBSTREAM_NAME_PREFIX}parent:beef"
    conv_id = await _seed(name, True)
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        got = await conversation_handlers.local_membership_hash(sess, convo)
    assert got == _expected_without_extra()


@pytest.mark.asyncio
async def test_an_active_ordinary_peer_does_change_the_hash() -> None:
    conv_id = await _seed("bob", True)
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        got = await conversation_handlers.local_membership_hash(sess, convo)
    assert got != _expected_without_extra()
    assert got == models.canonical_membership_hash(
        [OWN_CAP[32:], ALICE_CAP, EXTRA_CAP],
    )


@pytest.mark.asyncio
async def test_membership_hash_for_opens_its_own_session() -> None:
    conv_id = await _seed(None, True)
    got = await conversation_handlers.membership_hash_for(conv_id)
    assert got == _expected_without_extra()
    assert not models.is_membership_sentinel(got)


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
