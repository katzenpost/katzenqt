"""The corners of acknowledgements and their hand-over that the main tests
leave out: rosters that cannot be followed, inductions that cannot start,
replies that cannot be read, and members removed before they were numbered."""

from __future__ import annotations

import secrets
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from sqlmodel import select

from katzenqt import acks, models, persistent, removal, rosters, voucher
from katzenqt.headless import _actions
from tests.test_acks import (
    _Chat,
    _introducer,
    _reply_from,
    _two_members,
)

if TYPE_CHECKING:
    from katzenqt._thinclient import ThinClient


async def _lose_own_roster(chat: _Chat) -> None:
    """Leave our own roster hanging off an introducer nobody knows."""
    async with persistent.asession() as sess:
        own = await sess.get(
            persistent.RosterMember, (chat.conversation_id, chat.own_key)
        )
        assert own is not None
        own.base_roster = None
        own.base_introducer = b"?" * 32
        own.base_position = persistent.position_bytes(1)
        sess.add(own)
        await sess.commit()


@pytest.mark.asyncio
async def test_a_conversation_still_joining_does_not_start_a_roster() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        sess.add(
            persistent.PendingVoucher(
                role="joiner",
                conversation_id=chat.conversation_id,
                step="awaiting",
                voucher=b"v" * 32,
            )
        )
        await sess.commit()
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.ensure_own_roster(sess, conv) is None


@pytest.mark.asyncio
async def test_nothing_is_acknowledged_from_a_roster_we_cannot_follow() -> (
    None
):
    chat, _ = await _two_members(read_to=6)
    await _lose_own_roster(chat)

    message = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(message)
    assert message.acks is None


@pytest.mark.asyncio
async def test_a_roster_we_cannot_follow_is_not_handed_over() -> None:
    chat, _ = await _two_members(read_to=6)
    await _lose_own_roster(chat)

    reply = await _reply_from(chat)
    assert reply.rosters is None
    assert [e.display_name for e in reply.please_adds if e] == ["me", "bob"]


@pytest.mark.asyncio
async def test_an_induction_waits_for_our_queued_acknowledgements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _two_members(read_to=6)
    queued = await chat.queue(models.GroupChatMessage(version=0, text="hi"))
    asked: list[uuid.UUID] = []
    answers = iter([False, True])

    async def sent(pwal_id: uuid.UUID, *, deadline_s: float) -> bool:
        asked.append(pwal_id)
        return next(answers)

    monkeypatch.setattr(persistent, "wait_for_sent", sent)
    assert not await acks.wait_for_outgoing(
        chat.conversation_id, deadline_s=1.0
    )
    assert await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1.0)
    assert asked == [queued, queued]


@pytest.mark.asyncio
async def test_an_induction_that_cannot_start_leaves_acknowledgements_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _two_members(read_to=6)

    async def payload(*_a: object, **_k: object) -> voucher._JoinerPayload:
        return voucher._JoinerPayload(uuid.uuid4(), b"w", b"p", b"i")

    async def never(conversation_id: int, *, deadline_s: float) -> bool:
        assert conversation_id in acks.inducting
        return False

    monkeypatch.setattr(voucher, "_read_joiner_payload", payload)
    monkeypatch.setattr(acks, "wait_for_outgoing", never)
    with pytest.raises(RuntimeError, match="still being sent"):
        await voucher.derive_read_and_induct(
            cast("ThinClient", object()),
            chat.conversation_id,
            "carol",
            b"v" * 32,
        )
    assert chat.conversation_id not in acks.inducting

    message = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(message)
    assert message.acks is not None


@pytest.mark.asyncio
async def test_introducing_a_member_already_numbered_changes_nothing() -> (
    None
):
    chat, bob = await _two_members(read_to=6)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        await acks.introduced(sess, conv, bob + bytes(104), uuid.uuid4())
        await sess.commit()
        seen = (await sess.exec(select(persistent.IntroductionSeen))).all()
    assert seen == []


@pytest.mark.asyncio
async def test_a_roster_begun_alone_is_replaced_by_the_one_handed_over() -> (
    None
):
    introducer, bob, _ = await _introducer()
    reply = await _reply_from(introducer)

    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        assert await acks.ensure_own_roster(sess, conv) == newcomer.own_key
        assert await acks.adopt(sess, conv, newcomer.own_key, reply)
        await sess.commit()
        group = await acks.load_group(sess, newcomer.conversation_id)
    assert rosters.roster_of(group, newcomer.own_key) == (
        introducer.own_key,
        bob,
        newcomer.own_key,
    )


@pytest.mark.asyncio
async def test_a_member_removed_before_it_was_numbered_is_forgotten() -> None:
    chat, bob, carol = await _introducer()
    async with persistent.asession() as sess:
        peer = (
            await sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.name == "carol"
                )
            )
        ).first()
        assert peer is not None
    await removal.remove_peer(
        conversation_id=chat.conversation_id, peer_id=peer.id
    )

    async with persistent.asession() as sess:
        group = await acks.load_group(sess, chat.conversation_id)
    assert rosters.roster_of(group, chat.own_key) == (chat.own_key, bob)
    (token,) = group.introductions.values()
    assert group.introductions == {(bob, 9): token}
    assert token.startswith(b"retired:")
    assert carol not in group.bases


@pytest.mark.asyncio
async def test_a_joiner_skips_the_place_of_a_removed_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introducer, _, _ = await _introducer()
    await removal.remove_peer(
        conversation_id=introducer.conversation_id,
        peer_id=introducer.peers["bob"][0],
    )
    reply = await voucher._build_who_reply(introducer.conversation_id)
    assert reply.please_adds[1] is None

    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        sess.add(
            persistent.PendingVoucher(
                role="joiner",
                conversation_id=newcomer.conversation_id,
                step="awaiting",
                voucher=b"v" * 32,
                voucher_read_cap=b"\x09" * 136,
                box1_index=b"\x00" * 104,
                voucher_secret_key=b"k",
            )
        )
        await sess.commit()

    async def fake_read_box(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        return (b"sealed reply", b"\x00" * 104)

    monkeypatch.setattr(voucher, "_read_box", fake_read_box)

    class Opened:
        who_reply = reply.to_cbor()
        mutated_message_write_cap = secrets.token_bytes(168)

    class Connection:
        async def voucher_open(
            self,
            *,
            voucher_secret_key: bytes,
            sealed_reply: bytes,
            message_write_cap: bytes,
        ) -> Opened:
            return Opened()

    added = await voucher.await_and_open(
        cast("ThinClient", Connection()),
        newcomer.conversation_id,
    )
    assert sorted(added) == ["carol", "me"]


@pytest.mark.asyncio
async def test_a_reply_that_cannot_be_read_costs_only_the_rosters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    introducer, _, _ = await _introducer()
    reply = await _reply_from(introducer)
    newcomer = await _Chat().create()

    async def half_done(
        sess: persistent.AsyncSession,
        conv: persistent.Conversation,
        own: bytes,
        reply_who: models.GroupChatReplyWho,
    ) -> bool:
        sess.add(
            persistent.RosterMember(
                conversation_id=conv.id, member_key=own, base_roster=b"\x80"
            )
        )
        await sess.flush()
        raise ValueError("rosters do not add up")

    monkeypatch.setattr(acks, "adopt", half_done)
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        await voucher._adopt_rosters(
            sess, conv, newcomer.write_cap[32:], reply
        )
        await sess.commit()
        rows = (await sess.exec(select(persistent.RosterMember))).all()
    assert [row.conversation_id for row in rows] == [
        introducer.conversation_id
    ] * len(rows)
    assert "will not acknowledge" in caplog.text


@pytest.mark.asyncio
async def test_a_headless_message_too_large_for_one_box_gets_its_stream() -> (
    None
):
    chat, _ = await _two_members(read_to=6)
    before = await _write_caps()
    await _actions._queue_text(
        chat.conversation_id,
        models.GroupChatMessage(version=0, text="x" * 4000),
    )
    assert await _write_caps() == before + 1


async def _write_caps() -> int:
    async with persistent.asession() as sess:
        return len((await sess.exec(select(persistent.WriteCapWAL))).all())
