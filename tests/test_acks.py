"""Acknowledgements against a real database: what is attached to an
outgoing message, what an incoming one records, and the check a claimed
box must pass."""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import TYPE_CHECKING, cast

import cbor2
import pytest
from sqlmodel import select

from katzenqt import (
    ack_codec,
    acks,
    conversation_handlers,
    models,
    network,
    persistent,
    removal,
    rosters,
    voucher,
)
from tests.fakes.thinclient import FakeThinClient

if TYPE_CHECKING:
    from katzenqt._thinclient import ThinClient
from tests.test_network_fake import _set_up_read_flow, _set_up_write_flow


def _index(position: int) -> bytes:
    """A box index naming ``position``, random in the rest."""
    return position.to_bytes(8, "little") + secrets.token_bytes(96)


class _Chat:
    """A conversation of ours and the peers in it."""

    def __init__(self) -> None:
        self.write_cap = secrets.token_bytes(168)
        self.own_key = self.write_cap[32:64]
        self.stream = uuid.uuid4()
        self.conversation_id = 0
        self.own_peer_id = 0
        self.peers: dict[str, tuple[int, uuid.UUID, bytes]] = {}

    async def create(self) -> "_Chat":
        own_rcw = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.WriteCapWAL(
                    id=self.stream,
                    write_cap=self.write_cap,
                    next_index=_index(0),
                )
            )
            sess.add(
                persistent.ReadCapWAL(
                    id=own_rcw,
                    write_cap_id=self.stream,
                    read_cap=self.write_cap[32:],
                    next_index=_index(0),
                )
            )
            own = persistent.ConversationPeer(
                name="me",
                read_cap_id=own_rcw,
                active=False,
            )
            sess.add(own)
            await sess.commit()
            await sess.refresh(own)
            self.own_peer_id = own.id
            conv = persistent.Conversation(
                name="demo",
                own_peer_id=own.id,
                write_cap=self.stream,
            )
            sess.add(conv)
            await sess.commit()
            await sess.refresh(conv)
            self.conversation_id = conv.id
            sess.add(
                persistent.ConversationPeerLink(
                    conversation_peer_id=own.id,
                    conversation_id=conv.id,
                )
            )
            await sess.commit()
        return self

    async def add_peer(
        self, name: str, *, read_to: int | None = None
    ) -> bytes:
        """Add a peer we read. ``read_to`` is the last box read on its
        stream, if any. Returns its key."""
        read_cap = secrets.token_bytes(136)
        rcw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            last = None if read_to is None else _index(read_to)
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    read_cap=read_cap,
                    next_index=_index(0),
                    frontier_index=last,
                    last_read_index=last,
                )
            )
            peer = persistent.ConversationPeer(
                name=name,
                read_cap_id=rcw_id,
                active=True,
            )
            sess.add(peer)
            await sess.commit()
            await sess.refresh(peer)
            peer_id = peer.id
            sess.add(
                persistent.ConversationPeerLink(
                    conversation_peer_id=peer_id,
                    conversation_id=self.conversation_id,
                )
            )
            await sess.commit()
            self.peers[name] = (peer_id, rcw_id, read_cap[:32])
        return read_cap[:32]

    async def set_roster(
        self,
        owner: bytes,
        members: list[bytes],
        *,
        seen: int | None = None,
    ) -> None:
        async with persistent.asession() as sess:
            await sess.merge(
                persistent.RosterMember(
                    conversation_id=self.conversation_id,
                    member_key=owner,
                    base_roster=cbor2.dumps(members),
                    seen=None
                    if seen is None
                    else persistent.position_bytes(seen),
                )
            )
            await sess.commit()

    async def wrote(self, position: int) -> bytes:
        """Record a box of ours at ``position``; returns its index."""
        index = _index(position)
        async with persistent.asession() as sess:
            sess.add(
                persistent.SentBox(
                    conversation_id=self.conversation_id,
                    bacap_stream=self.stream,
                    box_index=index,
                    position=persistent.position_bytes(position),
                    written_at=0.0,
                )
            )
            own = await sess.get(
                persistent.RosterMember,
                (self.conversation_id, self.own_key),
            )
            assert own is not None
            own.seen = persistent.position_bytes(position)
            sess.add(own)
            await sess.commit()
        return index

    async def receive(
        self,
        sender: str,
        gcm: models.GroupChatMessage,
        position: int,
    ) -> None:
        """Deliver ``gcm`` as read at ``position`` on ``sender``'s stream."""
        peer_id, rcw_id, _ = self.peers[sender]
        async with persistent.asession() as sess:
            peer = await sess.get(persistent.ConversationPeer, peer_id)
            rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
            assert peer is not None and rcw is not None
            await conversation_handlers.dispatch(
                sess,
                peer,
                gcm,
                b"F" + gcm.to_cbor(),
                position=position,
            )
            await acks.box_read(sess, peer, rcw, _index(position))
            await sess.commit()

    async def acked_position(self, sender: str) -> int:
        async with persistent.asession() as sess:
            peer = await sess.get(
                persistent.ConversationPeer, self.peers[sender][0]
            )
            assert peer is not None
            return persistent.position_int(peer.acked_position)

    async def queue(self, gcm: models.GroupChatMessage) -> uuid.UUID:
        async with persistent.asession() as sess:
            conv = await sess.get(
                persistent.Conversation, self.conversation_id
            )
            assert conv is not None
            _, entries = await acks.serialize_with_acks(sess, conv, gcm)
            final_pwal_id = entries[-1].id
            for entry in entries:
                sess.add(entry)
            await sess.commit()
            return final_pwal_id


async def _two_members(*, read_to: int | None = 6) -> tuple[_Chat, bytes]:
    """Ourselves and bob, each holding the roster (me, bob)."""
    chat = await _Chat().create()
    bob = await chat.add_peer("bob", read_to=read_to)
    await chat.set_roster(chat.own_key, [chat.own_key, bob])
    await chat.set_roster(bob, [chat.own_key, bob])
    return chat, bob


@pytest.mark.asyncio
async def test_a_founder_alone_starts_a_roster_of_itself() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.enabled(sess, conv) is None
        assert await acks.ensure_own_roster(sess, conv) == chat.own_key
        await sess.commit()
        group = await acks.load_group(sess, chat.conversation_id)
    assert group.bases == {chat.own_key: (chat.own_key,)}


@pytest.mark.asyncio
async def test_a_conversation_from_before_rosters_gets_none() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob", read_to=3)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.ensure_own_roster(sess, conv) is None

    gcm = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(gcm)
    assert gcm.acks is None


@pytest.mark.asyncio
async def test_a_conversation_joined_through_a_voucher_does_not_start_one() -> (
    None
):
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        conv.voucher_used = True
        sess.add(conv)
        await sess.flush()
        assert await acks.ensure_own_roster(sess, conv) is None


@pytest.mark.asyncio
async def test_a_message_carries_what_was_read_since_the_last_one() -> None:
    chat, bob = await _two_members(read_to=6)

    first = models.GroupChatMessage(version=0, text="one")
    pwal_id = await chat.queue(first)
    assert first.acks is not None
    named = ack_codec.decode(first.acks)
    assert list(named) == [1]
    assert persistent.box_position(named[1]) == 6

    async with persistent.asession() as sess:
        queued = await sess.get(persistent.OutgoingAcks, pwal_id)
        assert queued is not None
        assert queued.acker_key == chat.own_key
        assert cbor2.loads(queued.levels) == {1: 6}

    second = models.GroupChatMessage(version=0, text="two")
    await chat.queue(second)
    assert second.acks is None


@pytest.mark.asyncio
async def test_a_member_not_yet_numbered_is_not_acknowledged() -> None:
    chat, _ = await _two_members(read_to=None)
    await chat.add_peer("carol", read_to=4)
    gcm = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(gcm)
    assert gcm.acks is None


@pytest.mark.asyncio
async def test_uploads_and_introductions_carry_no_acknowledgements() -> None:
    chat, _ = await _two_members(read_to=6)
    upload = models.GroupChatMessage(
        version=0,
        file_upload=models.GroupChatFileUpload(
            payload=b"x",
            filetype="arbitrary",
            basename="a.bin",
        ),
    )
    await chat.queue(upload)
    assert upload.acks is None

    intro = models.GroupChatMessage(
        version=0,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(
            display_name="carol",
            read_cap=secrets.token_bytes(136),
        ),
    )
    await chat.queue(intro)
    assert intro.acks is None


@pytest.mark.asyncio
async def test_nothing_is_acknowledged_while_a_new_member_is_being_introduced() -> (
    None
):
    chat, _ = await _two_members(read_to=6)
    acks.inducting.add(chat.conversation_id)
    try:
        held = models.GroupChatMessage(version=0, text="hi")
        await chat.queue(held)
        assert held.acks is None
    finally:
        acks.inducting.discard(chat.conversation_id)

    async with persistent.asession() as sess:
        sess.add(
            persistent.IntroductionSeen(
                conversation_id=chat.conversation_id,
                introducer_key=chat.own_key,
                member_key=b"n" * 32,
                pending_pwal=uuid.uuid4(),
            )
        )
        await sess.commit()
    unwritten = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(unwritten)
    assert unwritten.acks is None


@pytest.mark.asyncio
async def test_an_acknowledgement_of_a_box_we_wrote_is_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat, _ = await _two_members()
    ours = await chat.wrote(11)

    caplog.set_level(logging.INFO, logger="katzenqt.acks")
    gcm = models.GroupChatMessage(
        version=0,
        text="got it",
        acks=ack_codec.encode({0: ours}),
    )
    await chat.receive("bob", gcm, position=7)

    assert await chat.acked_position("bob") == 11
    assert "ACKED" in caplog.text and "peer=bob position=11" in caplog.text


@pytest.mark.asyncio
async def test_an_acknowledgement_of_a_box_we_never_wrote_is_ignored() -> (
    None
):
    chat, _ = await _two_members()
    await chat.wrote(11)

    forged = models.GroupChatMessage(
        version=0,
        text="hm",
        acks=ack_codec.encode({0: _index(11)}),
    )
    await chat.receive("bob", forged, position=7)
    assert await chat.acked_position("bob") == -1


@pytest.mark.asyncio
async def test_an_acknowledgement_naming_someone_else_is_not_ours() -> None:
    chat, _ = await _two_members()
    ours = await chat.wrote(11)
    elsewhere = models.GroupChatMessage(
        version=0,
        text="hm",
        acks=ack_codec.encode({1: ours}),
    )
    await chat.receive("bob", elsewhere, position=7)
    assert await chat.acked_position("bob") == -1


@pytest.mark.asyncio
async def test_an_older_acknowledgement_does_not_move_the_mark_back() -> None:
    chat, _ = await _two_members()
    early = await chat.wrote(11)
    late = await chat.wrote(12)
    for position, value in ((7, late), (8, early)):
        gcm = models.GroupChatMessage(
            version=0,
            text="ok",
            acks=ack_codec.encode({0: value}),
        )
        await chat.receive("bob", gcm, position=position)
    assert await chat.acked_position("bob") == 12


@pytest.mark.asyncio
async def test_a_malformed_field_costs_only_the_acknowledgements() -> None:
    chat, _ = await _two_members()
    await chat.wrote(11)
    gcm = models.GroupChatMessage(
        version=0, text="still shown", acks=b"\x05\x03"
    )
    await chat.receive("bob", gcm, position=7)
    assert await chat.acked_position("bob") == -1
    async with persistent.asession() as sess:
        log = (await sess.exec(select(persistent.ConversationLog))).all()
        assert len(log) == 1


@pytest.mark.asyncio
async def test_an_acknowledgement_waits_for_its_senders_roster() -> None:
    """Bob acknowledges us under an index we cannot place until we have
    read the Introduction that makes his roster reach that far."""
    chat = await _Chat().create()
    bob = await chat.add_peer("bob")
    carol = b"c" * 32
    await chat.set_roster(chat.own_key, [chat.own_key, bob])
    await chat.set_roster(bob, [bob])
    ours = await chat.wrote(11)

    gcm = models.GroupChatMessage(
        version=0,
        text="hi",
        acks=ack_codec.encode({1: ours}),
    )
    await chat.receive("bob", gcm, position=3)
    assert await chat.acked_position("bob") == -1

    await chat.set_roster(bob, [bob, chat.own_key, carol], seen=3)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        await acks.claim(sess, conv, chat.own_key)
        await sess.commit()
    assert await chat.acked_position("bob") == 11


@pytest.mark.asyncio
async def test_an_introduction_read_is_recorded_with_its_position() -> None:
    chat, bob = await _two_members()
    carol_cap = secrets.token_bytes(136)
    intro = models.GroupChatMessage(
        version=0,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(
            display_name="carol",
            read_cap=carol_cap,
        ),
    )
    await chat.receive("bob", intro, position=9)
    await chat.receive("bob", intro, position=9)

    async with persistent.asession() as sess:
        group = await acks.load_group(sess, chat.conversation_id)
    assert group.introductions == {(bob, 9): carol_cap[:32]}
    assert group.bases[carol_cap[:32]] == rosters.Inherited(bob, 9)
    assert group.seen[bob] == 9
    assert rosters.roster_of(group, carol_cap[:32]) == (
        chat.own_key,
        bob,
        carol_cap[:32],
    )


@pytest.mark.asyncio
async def test_our_roster_grows_once_the_message_is_written() -> None:
    """We read bob's Introduction of carol and acknowledge past it. Carol is
    numbered in that message, at the position it is written at."""
    chat, bob = await _two_members(read_to=None)
    carol_cap = secrets.token_bytes(136)
    intro = models.GroupChatMessage(
        version=0,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(
            display_name="carol",
            read_cap=carol_cap,
        ),
    )
    await chat.receive("bob", intro, position=9)

    gcm = models.GroupChatMessage(version=0, text="welcome")
    pwal_id = await chat.queue(gcm)
    assert gcm.acks is not None

    async with persistent.asession() as sess:
        before = await acks.load_group(sess, chat.conversation_id)
    assert rosters.roster_of(before, chat.own_key) == (chat.own_key, bob)

    written_at = _index(4)
    with persistent.Session(persistent._engine_sync) as sync:
        pwal = sync.get(persistent.PlaintextWAL, pwal_id)
        assert pwal is not None
        persistent._record_sent_box(sync, written_at, pwal)
        persistent._record_sent_box(sync, written_at, pwal)
        sync.commit()

    async with persistent.asession() as sess:
        after = await acks.load_group(sess, chat.conversation_id)
        boxes = (await sess.exec(select(persistent.SentBox))).all()
        assert await sess.get(persistent.OutgoingAcks, pwal_id) is None
    assert [box.box_index for box in boxes] == [written_at]
    assert after.acks == {(chat.own_key, 4): {1: 9}}
    assert after.seen[chat.own_key] == 4
    assert rosters.roster_of(after, chat.own_key) == (
        chat.own_key,
        bob,
        carol_cap[:32],
    )


@pytest.mark.asyncio
async def test_a_stream_is_not_acknowledged_past_a_message_still_arriving() -> (
    None
):
    chat, _ = await _two_members(read_to=None)
    peer_id, rcw_id, _ = chat.peers["bob"]
    substream_cap = secrets.token_bytes(136)
    async with persistent.asession() as sess:
        sess.add(
            persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=substream_cap)
        )
        sess.add(
            persistent.ReceivedPiece(
                read_cap=rcw_id,
                bacap_index=(5).to_bytes(8, "little"),
                chunk_type=b"I",
                chunk=substream_cap,
            )
        )
        await sess.commit()

    await chat.receive(
        "bob", models.GroupChatMessage(version=0, text="later"), 6
    )
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert rcw is not None
        assert rcw.frontier_index is not None
        assert persistent.box_position(rcw.frontier_index) == 6
        assert rcw.last_read_index is None

        for piece in (
            await sess.exec(select(persistent.ReceivedPiece))
        ).all():
            await sess.delete(piece)
        await sess.flush()
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        assert peer is not None
        await acks.settle(sess, peer, rcw)
        assert rcw.last_read_index == rcw.frontier_index
        await sess.commit()


@pytest.mark.asyncio
async def test_a_failed_transfer_does_not_hold_acknowledgements_back() -> (
    None
):
    chat, _ = await _two_members(read_to=None)
    _, rcw_id, _ = chat.peers["bob"]
    substream_cap = secrets.token_bytes(136)
    async with persistent.asession() as sess:
        sess.add(
            persistent.ReadCapWAL(
                id=uuid.uuid4(),
                read_cap=substream_cap,
                substream_failure="gone",
            )
        )
        sess.add(
            persistent.ReceivedPiece(
                read_cap=rcw_id,
                bacap_index=(5).to_bytes(8, "little"),
                chunk_type=b"I",
                chunk=substream_cap,
            )
        )
        await sess.commit()
    await chat.receive(
        "bob", models.GroupChatMessage(version=0, text="later"), 6
    )
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert rcw is not None and rcw.last_read_index is not None
        assert persistent.box_position(rcw.last_read_index) == 6


@pytest.mark.asyncio
async def test_a_confirmed_write_is_recorded_as_a_sent_box(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_write_flow(fake_thinclient)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    await network.drain_mixwal_write_single(
        fake_thinclient,
        mw,
        {setup["bacap_stream"]},
    )
    async with persistent.asession() as sess:
        boxes = (await sess.exec(select(persistent.SentBox))).all()
    assert len(boxes) == 1
    assert boxes[0].box_index == setup["first_message_index"]
    assert boxes[0].conversation_id == setup["conversation_id"]
    assert persistent.position_int(
        boxes[0].position
    ) == persistent.box_position(setup["first_message_index"])


@pytest.mark.asyncio
async def test_a_box_read_moves_what_may_be_acknowledged(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_read_flow(fake_thinclient)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    await network.drain_mixwal_read_single(
        connection=fake_thinclient,
        rcw_read_cap=setup["read_cap"],
        mw=mw,
        draining_right_now={setup["bacap_stream"]},
    )
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw is not None
        assert rcw.frontier_index == setup["first_message_index"]
        assert rcw.last_read_index == setup["first_message_index"]
        assert rcw.acked_index is None


@pytest.mark.asyncio
async def test_a_text_message_is_queued_with_its_acknowledgements() -> None:
    chat, _ = await _two_members(read_to=6)
    gcm = models.GroupChatMessage(version=0, text="hello")
    upload = await acks.append_outbound_text(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=gcm,
    )
    assert upload is None
    async with persistent.asession() as sess:
        log = (await sess.exec(select(persistent.ConversationLog))).one()
        queued = (await sess.exec(select(persistent.PlaintextWAL))).one()
        owed = (await sess.exec(select(persistent.OutgoingAcks))).one()
        assert log.outgoing_pwal == queued.id == owed.pwal_id
        assert log.network_status == 1
        assert queued.bacap_stream == chat.stream
        sent = models.GroupChatMessage.from_cbor(queued.bacap_payload[1:])
    assert sent.text == "hello"
    assert sent.acks is not None and list(ack_codec.decode(sent.acks)) == [1]
    assert models.GroupChatMessage.from_cbor(log.payload[1:]) == sent


@pytest.mark.asyncio
async def test_a_message_too_large_for_one_box_still_carries_them() -> None:
    chat, _ = await _two_members(read_to=6)
    gcm = models.GroupChatMessage(version=0, text="x" * 4000)
    upload = await acks.append_outbound_text(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=gcm,
    )
    assert upload is not None
    async with persistent.asession() as sess:
        owed = (await sess.exec(select(persistent.OutgoingAcks))).one()
        announcing = await sess.get(persistent.PlaintextWAL, owed.pwal_id)
        assert announcing is not None
        assert announcing.bacap_stream == chat.stream
        assert announcing.indirection is not None
    assert gcm.acks is not None


async def _never_sent(pwal_id: uuid.UUID, *, deadline_s: float) -> bool:
    raise AssertionError(f"waited for {pwal_id}, which will never be sent")


@pytest.mark.asyncio
async def test_a_cancelled_message_gives_its_acknowledgements_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _two_members(read_to=6)
    upload = await acks.append_outbound_text(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=models.GroupChatMessage(version=0, text="x" * 4000),
    )
    assert upload is not None
    await network.cancel_upload(rcw_id=upload.rcw_id)

    async with persistent.asession() as sess:
        assert (await sess.exec(select(persistent.OutgoingAcks))).all() == []
    monkeypatch.setattr(persistent, "wait_for_sent", _never_sent)
    assert await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1.0)

    again = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(again)
    assert again.acks is not None and list(ack_codec.decode(again.acks)) == [
        1
    ]


@pytest.mark.asyncio
async def test_a_cancelled_message_leaves_a_later_acknowledgement_alone() -> (
    None
):
    chat, _ = await _two_members(read_to=6)
    upload = await acks.append_outbound_text(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=models.GroupChatMessage(version=0, text="x" * 4000),
    )
    assert upload is not None
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, chat.peers["bob"][1])
        assert rcw is not None
        rcw.last_read_index = _index(8)
        sess.add(rcw)
        await sess.commit()
    later = models.GroupChatMessage(version=0, text="read further")
    await chat.queue(later)
    assert later.acks is not None

    await network.cancel_upload(rcw_id=upload.rcw_id)

    again = models.GroupChatMessage(version=0, text="nothing new")
    await chat.queue(again)
    assert again.acks is None


@pytest.mark.asyncio
async def test_acknowledgements_left_by_a_dropped_message_are_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _two_members(read_to=6)
    queued = await chat.queue(models.GroupChatMessage(version=0, text="hi"))
    async with persistent.asession() as sess:
        pwal = await sess.get(persistent.PlaintextWAL, queued)
        assert pwal is not None
        await sess.delete(pwal)
        await sess.commit()

    monkeypatch.setattr(persistent, "wait_for_sent", _never_sent)
    assert await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1.0)
    async with persistent.asession() as sess:
        assert (await sess.exec(select(persistent.OutgoingAcks))).all() == []

    again = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(again)
    assert again.acks is not None


@pytest.mark.asyncio
async def test_acknowledgements_of_a_message_already_sent_are_not_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _two_members(read_to=6)
    queued = await chat.queue(models.GroupChatMessage(version=0, text="hi"))
    async with persistent.asession() as sess:
        pwal = await sess.get(persistent.PlaintextWAL, queued)
        assert pwal is not None
        await sess.delete(pwal)
        sess.add(
            persistent.SentLog(
                id=queued, conversation_id=chat.conversation_id
            )
        )
        await sess.commit()
    asked: list[uuid.UUID] = []

    async def sent(pwal_id: uuid.UUID, *, deadline_s: float) -> bool:
        asked.append(pwal_id)
        return True

    monkeypatch.setattr(persistent, "wait_for_sent", sent)
    assert await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1.0)
    assert asked == [queued]

    again = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(again)
    assert again.acks is None


@pytest.mark.asyncio
async def test_the_gui_path_queues_and_wakes_the_listeners() -> None:
    chat, _ = await _two_members(read_to=6)
    await network.notify_outbound_text_sent(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=models.GroupChatMessage(version=0, text="hello"),
    )
    assert network.conversation_update_queue.get_nowait() == (
        chat.conversation_id,
        False,
    )
    async with persistent.asession() as sess:
        assert (
            len((await sess.exec(select(persistent.OutgoingAcks))).all()) == 1
        )


async def _introducer() -> tuple[_Chat, bytes, bytes]:
    """We hold bob in our roster, and have read bob's Introduction of carol
    without yet numbering her. Bob has acknowledged our stream further than
    we know we have written."""
    chat, bob = await _two_members(read_to=9)
    carol_cap = secrets.token_bytes(136)
    intro = models.GroupChatMessage(
        version=0,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(
            display_name="carol",
            read_cap=carol_cap,
        ),
    )
    await chat.receive("bob", intro, position=9)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        voucher._add_peer(sess, conv, "carol", carol_cap)
        await sess.commit()
    await chat.wrote(3)
    ahead = models.GroupChatMessage(
        version=0,
        text="hi",
        acks=ack_codec.encode({0: _index(5)}),
    )
    await chat.receive("bob", ahead, position=10)
    return chat, bob, carol_cap[:32]


async def _reply_from(chat: _Chat) -> models.GroupChatReplyWho:
    reply = await voucher._build_who_reply(chat.conversation_id)
    return models.GroupChatReplyWho.from_cbor(reply.to_cbor())


@pytest.mark.asyncio
async def test_the_reply_lists_our_roster_then_whoever_is_not_yet_numbered() -> (
    None
):
    chat, bob, carol = await _introducer()
    reply = await _reply_from(chat)

    assert reply.roster_size == 2
    listed = [entry for entry in reply.please_adds if entry is not None]
    assert [entry.display_name for entry in listed] == ["me", "bob", "carol"]
    assert [entry.read_cap[:32] for entry in listed] == [
        chat.own_key,
        bob,
        carol,
    ]
    # Bob introduced carol, so his roster already holds her; ours does not.
    assert reply.rosters == [
        bytes([0, 1]),
        bytes([0, 1, 2]),
        bytes([0, 1, 2]),
    ]
    assert reply.seen == [3, 10, -1]
    assert reply.introductions == [(1, 9, 2)]
    assert reply.unsettled == [(1, 10, {0: 5})]


@pytest.mark.asyncio
async def test_a_new_member_carries_on_from_what_it_was_handed() -> None:
    introducer, bob, carol = await _introducer()
    reply = await _reply_from(introducer)

    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        for entry in reply.please_adds:
            assert entry is not None
            voucher._add_peer(sess, conv, entry.display_name, entry.read_cap)
        assert await acks.adopt(sess, conv, newcomer.own_key, reply)
        assert await acks.enabled(sess, conv) == newcomer.own_key
        await sess.commit()
        ours = await acks.load_group(sess, newcomer.conversation_id)
        theirs = await acks.load_group(sess, introducer.conversation_id)

    me = introducer.own_key
    assert rosters.roster_of(ours, newcomer.own_key) == (
        me,
        bob,
        newcomer.own_key,
    )
    for member in (me, bob, carol):
        assert rosters.follow(ours, member) == rosters.follow(theirs, member)
    assert rosters.roster_of(ours, carol) == (me, bob, carol)
    assert rosters.follow(ours, bob) == rosters.Followed(
        (me, bob, carol), ((10, {0: 5}),)
    )


@pytest.mark.asyncio
async def test_a_reply_without_rosters_leaves_the_conversation_without_them() -> (
    None
):
    newcomer = await _Chat().create()
    plain = models.GroupChatReplyWho(
        please_adds=[
            models.GroupChatPleaseAdd(
                display_name="alice", read_cap=b"\x05" * 136
            ),
        ]
    )
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        assert not await acks.adopt(sess, conv, newcomer.own_key, plain)
        assert await acks.enabled(sess, conv) is None


@pytest.mark.parametrize(
    "broken", [{"rosters": [b""]}, {"seen": [1]}, {"roster_size": 9}]
)
@pytest.mark.asyncio
async def test_a_reply_that_does_not_add_up_is_not_adopted(
    broken: dict[str, object],
) -> None:
    introducer, _, _ = await _introducer()
    reply = (await _reply_from(introducer)).model_copy(update=broken)
    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        assert not await acks.adopt(sess, conv, newcomer.own_key, reply)
        assert await acks.enabled(sess, conv) is None


@pytest.mark.asyncio
async def test_a_reply_that_already_lists_us_is_not_adopted() -> None:
    introducer, bob, _ = await _introducer()
    reply = await _reply_from(introducer)
    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(
            persistent.Conversation, newcomer.conversation_id
        )
        assert conv is not None
        assert not await acks.adopt(sess, conv, bob, reply)


@pytest.mark.asyncio
async def test_an_induction_waiting_for_its_voucher_holds_nothing_back() -> (
    None
):
    chat, _ = await _two_members(read_to=6)
    async with persistent.asession() as sess:
        sess.add(
            persistent.PendingVoucher(
                role="inductor",
                conversation_id=chat.conversation_id,
                step="inducting",
                voucher=b"w" * 32,
            )
        )
        await sess.commit()

    message = models.GroupChatMessage(version=0, text="hi")
    await chat.queue(message)
    assert message.acks is not None


@pytest.mark.asyncio
async def test_the_joiner_side_of_the_handshake_adopts_the_rosters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introducer, bob, _ = await _introducer()
    reply = await voucher._build_who_reply(introducer.conversation_id)

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
    mutated = secrets.token_bytes(168)

    async def fake_read_box(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        return (b"sealed reply", b"\x00" * 104)

    monkeypatch.setattr(voucher, "_read_box", fake_read_box)

    class Opened:
        who_reply = reply.to_cbor()
        mutated_message_write_cap = mutated

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
    assert sorted(added) == ["bob", "carol", "me"]
    async with persistent.asession() as sess:
        group = await acks.load_group(sess, newcomer.conversation_id)
    own = mutated[32:64]
    assert rosters.roster_of(group, own) == (introducer.own_key, bob, own)


@pytest.mark.asyncio
async def test_an_introduction_of_ours_numbers_the_member_once_written() -> (
    None
):
    chat, bob = await _two_members(read_to=None)
    dave_cap = secrets.token_bytes(136)
    final_pwal_id = await voucher._write_introduction_log(
        chat.conversation_id,
        "dave",
        dave_cap,
    )
    async with persistent.asession() as sess:
        queued = await acks.load_group(sess, chat.conversation_id)
    assert rosters.roster_of(queued, chat.own_key) == (chat.own_key, bob)
    assert queued.bases[dave_cap[:32]] == (chat.own_key, bob, dave_cap[:32])

    held = models.GroupChatMessage(version=0, text="not yet")
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, chat.peers["bob"][1])
        assert rcw is not None
        rcw.last_read_index = _index(2)
        sess.add(rcw)
        await sess.commit()
    await chat.queue(held)
    assert held.acks is None

    with persistent.Session(persistent._engine_sync) as sync:
        pwal = sync.get(persistent.PlaintextWAL, final_pwal_id)
        assert pwal is not None
        persistent._record_sent_box(sync, _index(7), pwal)
        sync.commit()
    async with persistent.asession() as sess:
        written = await acks.load_group(sess, chat.conversation_id)
    assert written.introductions == {(chat.own_key, 7): dave_cap[:32]}
    assert rosters.roster_of(written, chat.own_key) == (
        chat.own_key,
        bob,
        dave_cap[:32],
    )

    released = models.GroupChatMessage(version=0, text="now")
    await chat.queue(released)
    assert released.acks is not None


@pytest.mark.asyncio
async def test_a_removed_member_keeps_its_place_and_loses_its_key() -> None:
    chat, bob, carol = await _introducer()
    peer_id = chat.peers["bob"][0]
    await removal.remove_peer(
        conversation_id=chat.conversation_id, peer_id=peer_id
    )

    token = rosters.placeholder(1)
    async with persistent.asession() as sess:
        group = await acks.load_group(sess, chat.conversation_id)
        rows = (await sess.exec(select(persistent.RosterMember))).all()
        seen = (await sess.exec(select(persistent.IntroductionSeen))).all()
        levels = (await sess.exec(select(persistent.AckLevel))).all()
    assert rosters.roster_of(group, chat.own_key) == (chat.own_key, token)
    assert group.introductions == {(token, 9): carol}
    assert group.bases[carol] == rosters.Inherited(token, 9)
    stored = b"".join(
        b"".join(
            filter(
                None, (row.member_key, row.base_roster, row.base_introducer)
            )
        )
        for row in rows
    ) + b"".join(row.introducer_key + row.member_key for row in seen)
    assert bob not in stored
    assert {level.acker_key for level in levels} == {token}

    reply = await _reply_from(chat)
    assert reply.roster_size == 2
    assert reply.please_adds[1] is None
    assert [e.display_name for e in reply.please_adds if e] == ["me", "carol"]


@pytest.mark.asyncio
async def test_a_removed_conversation_leaves_no_roster_behind() -> None:
    chat, _, _ = await _introducer()
    await removal.remove_conversation(conversation_id=chat.conversation_id)
    async with persistent.asession() as sess:
        for table in (
            persistent.SentBox,
            persistent.RosterMember,
            persistent.IntroductionSeen,
            persistent.AckLevel,
            persistent.OutgoingAcks,
        ):
            assert (await sess.exec(select(table))).all() == []


@pytest.mark.asyncio
async def test_the_summary_names_our_roster_and_who_has_acknowledged_us() -> (
    None
):
    chat, _ = await _two_members()
    ours = await chat.wrote(11)
    gcm = models.GroupChatMessage(
        version=0,
        text="got it",
        acks=ack_codec.encode({0: ours}),
    )
    await chat.receive("bob", gcm, position=7)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.summary(sess, conv) == (["me", "bob"], {"bob": 11})

    await removal.remove_peer(
        conversation_id=chat.conversation_id,
        peer_id=chat.peers["bob"][0],
    )
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.summary(sess, conv) == (["me", "?"], {})

    plain = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, plain.conversation_id)
        assert conv is not None
        assert await acks.summary(sess, conv) == (None, {})
