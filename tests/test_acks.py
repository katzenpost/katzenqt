"""Acknowledgements and rosters against a real database: ``katzenqt.acks``
driving the group state machine as katzenqt reads and writes."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING, Any, cast

import cbor2
import pytest
from sqlmodel import Session, select

from katzenqt import (
    ack_codec,
    acks,
    conversation_handlers,
    models,
    network,
    persistent,
    removal,
    voucher,
)
from katzenqt.groupchat import state as group_state
from katzenqt.groupchat import wire
from katzenqt.groupchat.group import Group
from katzenqt.groupchat.numbering import Entry
from katzenqt.headless import _actions
from tests.fakes.thinclient import FakeThinClient
from tests.test_network_fake import _set_up_read_flow, _set_up_write_flow

if TYPE_CHECKING:
    from katzenqt._thinclient import ThinClient


def _index(n: int) -> bytes:
    """A stand-in ``MessageBoxIndex`` for a stream's nth box: opaque here,
    and the same every time, so a test can say which box it expects."""
    return hashlib.shake_256(b"box %d" % n).digest(104)


def _text(text: str = "hi") -> models.GroupChatMessage:
    return models.GroupChatMessage(version=models.GROUP_CHAT_VERSION, text=text)


def _introduction(name: str, read_cap: bytes) -> models.GroupChatMessage:
    return models.GroupChatMessage(
        version=models.GROUP_CHAT_VERSION,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(display_name=name, read_cap=read_cap),
    )


class _Chat:
    """A conversation of ours and the peers in it."""

    def __init__(self) -> None:
        self.write_cap = secrets.token_bytes(168)
        self.own_cap = self.write_cap[32:]
        self.own_key = self.own_cap[:32]
        self.stream = uuid.uuid4()
        self.conversation_id = 0
        self.own_peer_id = 0
        self.peers: dict[str, tuple[int, uuid.UUID, bytes]] = {}
        self.written = 0

    async def create(self, *, voucher_used: bool = False) -> "_Chat":
        own_rcw = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(
                id=self.stream, write_cap=self.write_cap, next_index=_index(100),
            ))
            sess.add(persistent.ReadCapWAL(
                id=own_rcw, write_cap_id=self.stream, read_cap=self.own_cap,
                next_index=_index(0),
            ))
            own = persistent.ConversationPeer(name="me", read_cap_id=own_rcw, active=False)
            sess.add(own)
            await sess.commit()
            await sess.refresh(own)
            self.own_peer_id = own.id
            conv = persistent.Conversation(
                name="demo", own_peer_id=own.id, write_cap=self.stream,
                voucher_used=voucher_used,
            )
            sess.add(conv)
            await sess.commit()
            await sess.refresh(conv)
            self.conversation_id = conv.id
            sess.add(persistent.ConversationPeerLink(
                conversation_peer_id=own.id, conversation_id=conv.id,
            ))
            await sess.commit()
        return self

    async def add_peer(self, name: str, read_cap: bytes | None = None) -> bytes:
        """Add a peer we read; returns its key."""
        read_cap = read_cap or secrets.token_bytes(136)
        rcw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.ReadCapWAL(
                id=rcw_id, read_cap=read_cap, next_index=read_cap[32:],
            ))
            peer = persistent.ConversationPeer(name=name, read_cap_id=rcw_id, active=True)
            sess.add(peer)
            await sess.commit()
            await sess.refresh(peer)
            peer_id = peer.id
            sess.add(persistent.ConversationPeerLink(
                conversation_peer_id=peer_id, conversation_id=self.conversation_id,
            ))
            await sess.commit()
            self.peers[name] = (peer_id, rcw_id, read_cap)
        return read_cap[:32]

    async def found(self, *names: str) -> None:
        """Start a group of us and ``names``, all founders."""
        caps = [self.own_cap] + [self.peers[n][2] for n in names]
        group = Group.found(self.own_cap, caps, codec=acks.CODEC)
        async with persistent.asession() as sess:
            sess.add(persistent.GroupState(
                conversation_id=self.conversation_id, state=group_state.dump(group),
            ))
            await sess.commit()

    async def group(self) -> Group:
        async with persistent.asession() as sess:
            stored = await acks._load(sess, self.conversation_id)
            assert stored is not None
            await sess.commit()
            return stored.group

    async def receive(self, sender: str, gcm: models.GroupChatMessage, n: int) -> None:
        """Deliver ``gcm`` as the network does: dispatched, then its box
        read, with the stream's next index moved past it."""
        peer_id, rcw_id, _ = self.peers[sender]
        async with persistent.asession() as sess:
            peer = await sess.get(persistent.ConversationPeer, peer_id)
            rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
            assert peer is not None and rcw is not None
            rcw.next_index = _index(n + 1)
            await conversation_handlers.dispatch(
                sess, peer, gcm, b"F" + gcm.to_cbor(), box=_index(n),
            )
            await acks.box_read(sess, peer, rcw, _index(n))
            await sess.commit()

    async def queue(self, gcm: models.GroupChatMessage) -> list[persistent.PlaintextWAL]:
        async with persistent.conversation_log_order_lock(self.conversation_id):
            async with persistent.asession() as sess:
                conv = await sess.get(persistent.Conversation, self.conversation_id)
                assert conv is not None
                _, entries = await acks.serialize_with_acks(sess, conv, gcm)
                for entry in entries:
                    sess.add(entry)
                await sess.commit()
                rows = [e for e in entries if isinstance(e, persistent.PlaintextWAL)]
                for row in rows:
                    await sess.refresh(row)
                return rows

    def land(self, pwal_id: uuid.UUID) -> bytes:
        """The courier acknowledges the write of ``pwal_id``, as the send
        path records it; returns the box index."""
        index = _index(self.written)
        self.written += 1
        with Session(persistent._engine_sync) as sess:
            pwal = sess.get(persistent.PlaintextWAL, pwal_id)
            assert pwal is not None
            persistent._ensure_sent_log_and_flip_status(sess, pwal)
            persistent._record_sent_box(sess, index, pwal)
            sess.delete(pwal)
            sess.commit()
        return index

    async def land_all(self) -> None:
        async with persistent.asession() as sess:
            queued = (await sess.exec(
                select(persistent.PlaintextWAL).where(
                    persistent.PlaintextWAL.bacap_stream == self.stream
                )
            )).all()
        for pwal in queued:
            self.land(pwal.id)


def _sent(pwal: persistent.PlaintextWAL) -> models.GroupChatMessage:
    return models.GroupChatMessage.from_cbor(pwal.bacap_payload[1:])


async def _with_bob() -> tuple[_Chat, bytes]:
    chat = await _Chat().create()
    bob = await chat.add_peer("bob")
    await chat.found("bob")
    return chat, bob


# Starting.


@pytest.mark.asyncio
async def test_a_founder_alone_starts_a_group_with_its_first_message() -> None:
    chat = await _Chat().create()
    (sent,) = await chat.queue(_text())
    assert _sent(sent).acks is None
    group = await chat.group()
    assert group.rosters.known(chat.own_key) == {0: chat.own_key}


@pytest.mark.asyncio
async def test_a_conversation_from_before_rosters_gets_none() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob")
    (sent,) = await chat.queue(_text())
    assert _sent(sent).acks is None
    async with persistent.asession() as sess:
        assert await sess.get(persistent.GroupState, chat.conversation_id) is None
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.hand_over(sess, conv) is None
        assert await acks.summary(sess, conv) == (None, {})


@pytest.mark.asyncio
async def test_a_conversation_joined_through_a_voucher_does_not_start_one() -> None:
    chat = await _Chat().create(voucher_used=True)
    await chat.queue(_text())
    async with persistent.asession() as sess:
        assert await sess.get(persistent.GroupState, chat.conversation_id) is None


@pytest.mark.asyncio
async def test_a_joiner_waiting_on_its_voucher_does_not_start_one() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role="joiner", conversation_id=chat.conversation_id, step="awaiting",
            voucher=b"v" * 32, voucher_read_cap=b"\x09" * 136, box1_index=b"\x00" * 104,
            voucher_secret_key=b"k",
        ))
        await sess.commit()
    await chat.queue(_text())
    async with persistent.asession() as sess:
        assert await sess.get(persistent.GroupState, chat.conversation_id) is None


# Acknowledging.


@pytest.mark.asyncio
async def test_a_message_carries_what_was_read_since_the_last_one() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text("one"), 3)
    (first,) = await chat.queue(_text())
    acked = ack_codec.decode(_sent(first).acks or b"")
    bob_place = (await chat.group()).rosters.index_of(chat.own_key, bob)
    assert bob_place is not None and list(acked) == [bob_place]
    assert acked[bob_place] == _index(3)
    (second,) = await chat.queue(_text())
    assert _sent(second).acks is None


@pytest.mark.asyncio
async def test_a_box_that_cannot_be_read_still_counts_as_read() -> None:
    chat, bob = await _with_bob()
    _, rcw_id, _ = chat.peers["bob"]
    peer_id = chat.peers["bob"][0]
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _index(5)
        await acks.box_read(sess, peer, rcw, _index(4))
        await sess.commit()
    (sent,) = await chat.queue(_text())
    acked = ack_codec.decode(_sent(sent).acks or b"")
    assert list(acked.values()) == [_index(4)]


@pytest.mark.asyncio
async def test_a_stream_is_not_acknowledged_past_a_message_still_arriving() -> None:
    chat, bob = await _with_bob()
    peer_id, rcw_id, _ = chat.peers["bob"]
    substream_cap = secrets.token_bytes(136)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=substream_cap))
        sess.add(persistent.ReceivedPiece(
            read_cap=rcw_id, bacap_index=(5).to_bytes(8, "little"),
            chunk_type=b"I", chunk=substream_cap,
        ))
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _index(6)
        await acks.box_read(sess, peer, rcw, _index(5), substream=substream_cap)
        await sess.commit()
    await chat.receive("bob", _text("later"), 6)
    (held,) = await chat.queue(_text())
    assert _sent(held).acks is None

    async with persistent.asession() as sess:
        for piece in (await sess.exec(select(persistent.ReceivedPiece))).all():
            await sess.delete(piece)
        await sess.flush()
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        await conversation_handlers.dispatch(
            sess, peer, _text("big"), b"F", substream=substream_cap,
        )
        await acks.settle(sess, peer, rcw)
        await sess.commit()
    (released,) = await chat.queue(_text())
    acked = ack_codec.decode(_sent(released).acks or b"")
    assert list(acked.values()) == [_index(6)]


@pytest.mark.asyncio
async def test_a_message_given_up_on_releases_what_waited_on_it() -> None:
    chat, bob = await _with_bob()
    peer_id, rcw_id, _ = chat.peers["bob"]
    substream_cap = secrets.token_bytes(136)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=substream_cap))
        sess.add(persistent.ReceivedPiece(
            read_cap=rcw_id, bacap_index=b"8 bytes.", chunk_type=b"I", chunk=substream_cap,
        ))
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _index(6)
        await acks.box_read(sess, peer, rcw, _index(5), substream=substream_cap)
        await sess.commit()
    (held,) = await chat.queue(_text())
    assert _sent(held).acks is None
    async with persistent.asession() as sess:
        for piece in (await sess.exec(select(persistent.ReceivedPiece))).all():
            await sess.delete(piece)
        await sess.flush()
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        await acks.settle(sess, peer, rcw)
        await sess.commit()
    (released,) = await chat.queue(_text())
    assert list(ack_codec.decode(_sent(released).acks or b"").values()) == [_index(5)]


@pytest.mark.asyncio
async def test_a_failed_transfer_does_not_hold_acknowledgements_back() -> None:
    chat, bob = await _with_bob()
    _, rcw_id, _ = chat.peers["bob"]
    substream_cap = secrets.token_bytes(136)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(
            id=uuid.uuid4(), read_cap=substream_cap, substream_failure="gone",
        ))
        sess.add(persistent.ReceivedPiece(
            read_cap=rcw_id, bacap_index=(5).to_bytes(8, "little"),
            chunk_type=b"I", chunk=substream_cap,
        ))
        peer = await sess.get(persistent.ConversationPeer, chat.peers["bob"][0])
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _index(6)
        await acks.box_read(sess, peer, rcw, _index(5), substream=substream_cap)
        await sess.commit()
    (sent,) = await chat.queue(_text())
    assert list(ack_codec.decode(_sent(sent).acks or b"").values()) == [_index(5)]


@pytest.mark.asyncio
async def test_settling_a_stream_we_do_not_follow_does_nothing() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob")
    peer_id, rcw_id, _ = chat.peers["bob"]
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        await acks.settle(sess, peer, rcw)
        await acks.box_read(sess, peer, rcw, _index(1))
        await acks.on_message(sess, peer, _text(), box=_index(1))
        rcw.read_cap = None
        await acks.settle(sess, peer, rcw)
        await acks.box_read(sess, peer, rcw, _index(1))
        await acks.on_message(sess, peer, _text(), box=_index(1))


# The Sent-box check.


@pytest.mark.asyncio
async def test_an_acknowledgement_of_a_box_we_wrote_is_accepted() -> None:
    chat, bob = await _with_bob()
    (sent,) = await chat.queue(_text())
    index = chat.land(sent.id)
    me = (await chat.group()).rosters.index_of(bob, chat.own_key)
    assert me is not None
    await chat.receive("bob", models.GroupChatMessage(
        version=2, text="seen", acks=ack_codec.encode({me: index}),
    ), 1)
    group = await chat.group()
    assert group.ledger.acked_by == {bob: 0}
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        names, acked = await acks.summary(sess, conv)
    assert sorted(names or ()) == ["bob", "me"]
    assert acked == {"bob": 0}


@pytest.mark.asyncio
async def test_an_acknowledgement_of_a_box_we_never_wrote_is_ignored() -> None:
    chat, bob = await _with_bob()
    me = (await chat.group()).rosters.index_of(bob, chat.own_key)
    assert me is not None
    await chat.receive("bob", models.GroupChatMessage(
        version=2, text="seen", acks=ack_codec.encode({me: _index(0)}),
    ), 1)
    assert (await chat.group()).ledger.acked_by == {}


# Numbering.


@pytest.mark.asyncio
async def test_a_member_introduced_is_numbered_in_a_control_message_first() -> None:
    chat, bob = await _with_bob()
    carol_cap = secrets.token_bytes(136)
    await chat.receive("bob", _introduction("carol", carol_cap), 4)
    control, content = await chat.queue(_text())
    assert content.after_id == control.id
    sent = _sent(control)
    assert sent.msg_type is models.GroupChatTypeEnum.CONTROL
    numbers = wire.decode_numbers(sent.numbers or b"")
    assert [index for index, _ in numbers] == [2]
    assert _sent(content).acks is None
    group = await chat.group()
    assert 2 not in group.rosters.known(chat.own_key)
    chat.land(control.id)
    assert (await chat.group()).rosters.known(chat.own_key)[2] == carol_cap[:32]


@pytest.mark.asyncio
async def test_a_control_message_is_not_shown() -> None:
    chat, bob = await _with_bob()
    control = models.GroupChatMessage(version=2, msg_type=models.GroupChatTypeEnum.CONTROL)
    peer_id, _, _ = chat.peers["bob"]
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        assert peer is not None
        result = await conversation_handlers.dispatch(sess, peer, control, b"F")
    assert not result.convlog_added


@pytest.mark.asyncio
async def test_a_message_too_large_for_one_box_comes_after_its_control_message() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _introduction("carol", secrets.token_bytes(136)), 4)
    rows = await chat.queue(_text("x" * 4000))
    on_our_stream = [r for r in rows if r.bacap_stream == chat.stream]
    control, announcing = on_our_stream
    assert announcing.indirection is not None
    assert announcing.after_id == control.id


# Giving acknowledgements back.


@pytest.mark.asyncio
async def test_a_cancelled_message_gives_its_acknowledgements_back() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text(), 3)
    (cancelled,) = await chat.queue(_text())
    async with persistent.conversation_log_order_lock(chat.conversation_id):
        async with persistent.asession() as sess:
            await acks.forget_outgoing(sess, chat.conversation_id, cancelled.id)
            await acks.forget_outgoing(sess, chat.conversation_id, uuid.uuid4())
            await sess.commit()
    (again,) = await chat.queue(_text())
    assert _sent(again).acks is not None


@pytest.mark.asyncio
async def test_waiting_for_what_is_owed_clears_what_was_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text(), 3)
    (dropped,) = await chat.queue(_text())
    (owed,) = await chat.queue(_text("two"))
    async with persistent.asession() as sess:
        row = await sess.get(persistent.PlaintextWAL, dropped.id)
        await sess.delete(row)
        await sess.commit()
    waited: list[uuid.UUID] = []

    async def sent(pwal_id: uuid.UUID, *, deadline_s: float, epoch_s: float) -> bool:
        waited.append(pwal_id)
        return True

    monkeypatch.setattr(persistent, "wait_for_sent", sent)
    assert await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1)
    assert waited == [owed.id]
    (again,) = await chat.queue(_text())
    assert _sent(again).acks is not None

    async def never(pwal_id: uuid.UUID, *, deadline_s: float, epoch_s: float) -> bool:
        return False

    monkeypatch.setattr(persistent, "wait_for_sent", never)
    assert not await acks.wait_for_outgoing(chat.conversation_id, deadline_s=1)
    lone = await _Chat().create()
    assert await acks.wait_for_outgoing(lone.conversation_id, deadline_s=1)


# Inducting.


async def _reply(chat: _Chat) -> models.GroupChatReplyWho:
    reply = await voucher._build_who_reply(chat.conversation_id)
    return models.GroupChatReplyWho.from_cbor(reply.to_cbor())


@pytest.mark.asyncio
async def test_an_induction_holds_acknowledgements_back_until_it_ends() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text(), 3)
    async with acks.held_back(chat.conversation_id):
        (held,) = await chat.queue(_text())
        assert _sent(held).acks is None
    (after,) = await chat.queue(_text())
    assert _sent(after).acks is not None


@pytest.mark.asyncio
async def test_an_induction_left_by_a_process_that_has_gone_is_released() -> None:
    chat, bob = await _with_bob()
    group = await chat.group()
    group.begin_induction()
    async with persistent.asession() as sess:
        row = await sess.get(persistent.GroupState, chat.conversation_id)
        assert row is not None
        row.state = group_state.dump(group)
        sess.add(row)
        await sess.commit()
    assert not (await chat.group()).ledger.holding


@pytest.mark.asyncio
async def test_owed_numbering_goes_out_before_an_induction() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _introduction("carol", secrets.token_bytes(136)), 4)
    async with acks.held_back(chat.conversation_id):
        async with persistent.asession() as sess:
            (queued,) = (await sess.exec(select(persistent.PlaintextWAL))).all()
        assert _sent(queued).numbers is not None


@pytest.mark.asyncio
async def test_a_new_member_carries_on_from_what_it_was_handed() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text(), 3)
    async with acks.held_back(chat.conversation_id):
        reply = await _reply(chat)
        dave_cap = secrets.token_bytes(136)
        final = await voucher._write_introduction_log(chat.conversation_id, "dave", dave_cap)
    async with persistent.asession() as sess:
        intro = await sess.get(persistent.PlaintextWAL, final)
        assert intro is not None
        numbers = wire.decode_numbers(_sent(intro).numbers or b"")
    assert numbers == ((2, None),)
    assert reply.newcomer == 2 and reply.introducer is not None
    bob_slot = reply.please_adds[1 - reply.introducer]
    assert bob_slot is not None and bob_slot.read_cap[32:] == _index(4)

    joiner = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, joiner.conversation_id)
        assert conv is not None
        assert await acks.adopt(sess, conv, dave_cap, reply)
        await sess.commit()
    async with persistent.asession() as sess:
        stored = await acks._load(sess, joiner.conversation_id)
        assert stored is not None
    roster = stored.group.rosters.known(dave_cap[:32])
    assert set(roster.values()) == {chat.own_key, bob, dave_cap[:32]}
    assert roster[2] == dave_cap[:32]


@pytest.mark.asyncio
async def test_the_joiner_side_of_the_handshake_adopts_the_rosters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introducer, bob = await _with_bob()
    reply = await voucher._build_who_reply(introducer.conversation_id)
    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role="joiner", conversation_id=newcomer.conversation_id, step="awaiting",
            voucher=b"v" * 32, voucher_read_cap=b"\x09" * 136, box1_index=b"\x00" * 104,
            voucher_secret_key=b"k",
        ))
        await sess.commit()
    mutated = secrets.token_bytes(168)

    async def fake_read_box(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        return (b"sealed reply", b"\x00" * 104)

    monkeypatch.setattr(voucher, "_read_box", fake_read_box)

    class Opened:
        who_reply = reply.to_cbor()
        mutated_message_write_cap = mutated

    class Connection:
        async def voucher_open(self, **_: object) -> Opened:
            return Opened()

    added = await voucher.await_and_open(
        cast("ThinClient", Connection()), newcomer.conversation_id,
    )
    assert sorted(added) == ["bob", "me"]
    async with persistent.asession() as sess:
        stored = await acks._load(sess, newcomer.conversation_id)
        assert stored is not None
    roster = stored.group.rosters.known(mutated[32:64])
    assert set(roster.values()) == {introducer.own_key, bob, mutated[32:64]}
    assert stored.group.read_cap == mutated[32:]


def _alice_and_bob_reply() -> models.GroupChatReplyWho:
    alice = models.GroupChatPleaseAdd(display_name="alice", read_cap=b"\x01" * 136)
    bob = models.GroupChatPleaseAdd(display_name="bob", read_cap=b"\x02" * 136)
    return models.GroupChatReplyWho(
        please_adds=[alice, bob],
        rosters=[[(0, 0), (1, 1)], None],
        introductions=[(0, bytes(104), 1), (5, bytes(104), 1), (0, b"short", 1)],
        introducer=0,
        newcomer=2,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"rosters": None},
        {"introducer": None},
        {"newcomer": 3},
        {"introducer": 5},
        {"rosters": [None]},
        {"rosters": [[(300, 0)], None]},
        {"rosters": [[(0, 9)], None]},
        {"please_adds": [None, models.GroupChatPleaseAdd(display_name="b", read_cap=b"\x02" * 136)]},
        {"please_adds": [
            models.GroupChatPleaseAdd(display_name="a", read_cap=b"\x02" * 136),
            models.GroupChatPleaseAdd(display_name="b", read_cap=b"\x02" * 136),
        ]},
    ],
)
@pytest.mark.asyncio
async def test_a_reply_that_does_not_add_up_is_not_adopted(change: dict[str, Any]) -> None:
    chat = await _Chat().create()
    reply = _alice_and_bob_reply().model_copy(update=change)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert not await acks.adopt(sess, conv, chat.own_cap, reply)


@pytest.mark.asyncio
async def test_a_reply_that_already_lists_us_is_not_adopted() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert not await acks.adopt(sess, conv, b"\x02" * 136, _alice_and_bob_reply())
        assert await acks.adopt(sess, conv, chat.own_cap, _alice_and_bob_reply())
        assert await acks.adopt(sess, conv, chat.own_cap, _alice_and_bob_reply())


@pytest.mark.asyncio
async def test_the_reply_leaves_out_a_member_it_cannot_name() -> None:
    chat, bob = await _with_bob()
    group = await chat.group()
    group.rosters.number(chat.own_key, 2, Entry(b"\x07" * 32))
    group.rosters.number(chat.own_key, 3, Entry())
    async with persistent.asession() as sess:
        row = await sess.get(persistent.GroupState, chat.conversation_id)
        assert row is not None
        row.state = group_state.dump(group)
        sess.add(row)
        await sess.commit()
    reply = await _reply(chat)
    assert reply.please_adds[2:] == [None, None]


@pytest.mark.asyncio
async def test_no_reply_without_a_next_index_on_our_stream() -> None:
    chat, bob = await _with_bob()
    async with persistent.asession() as sess:
        wcw = await sess.get(persistent.WriteCapWAL, chat.stream)
        assert wcw is not None
        wcw.next_index = None
        sess.add(wcw)
        await sess.commit()
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert await acks.hand_over(sess, conv) is None


@pytest.mark.asyncio
async def test_an_introduction_without_a_group_is_sent_as_it_is() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob")
    final = await voucher._write_introduction_log(
        chat.conversation_id, "dave", secrets.token_bytes(136),
    )
    async with persistent.asession() as sess:
        intro = await sess.get(persistent.PlaintextWAL, final)
        assert intro is not None and _sent(intro).numbers is None


# Removing.


@pytest.mark.asyncio
async def test_a_removed_member_keeps_its_place_but_not_its_name() -> None:
    chat, bob = await _with_bob()
    await chat.receive("bob", _text(), 3)
    peer_id = chat.peers["bob"][0]
    await removal.remove_peer(conversation_id=chat.conversation_id, peer_id=peer_id)
    group = await chat.group()
    assert bob not in group.readers
    assert len(group.rosters.known(chat.own_key)) == 2
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        names, _ = await acks.summary(sess, conv)
        await acks.forget_member(sess, conv, secrets.token_bytes(136))
    assert sorted(names or ()) == ["?", "me"]
    lone = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, lone.conversation_id)
        assert conv is not None
        await acks.forget_member(sess, conv, secrets.token_bytes(136))


@pytest.mark.asyncio
async def test_removing_a_conversation_removes_its_group_state() -> None:
    chat, _ = await _with_bob()
    await removal.remove_conversation(conversation_id=chat.conversation_id)
    async with persistent.asession() as sess:
        assert await sess.get(persistent.GroupState, chat.conversation_id) is None


# Time.


@pytest.mark.asyncio
async def test_records_older_than_the_window_are_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _with_bob()
    (sent,) = await chat.queue(_text())
    chat.land(sent.id)
    assert (await chat.group()).sent.records
    now = acks._replica_epoch()
    monkeypatch.setattr(acks, "_replica_epoch", lambda: now + 10)
    assert not (await chat.group()).sent.records


# The codec.


def test_the_codec_reads_what_it_writes() -> None:
    control = wire.Message(
        numbers=((2, b"\x07" * 8),), introduced_at=wire.IntroRef(1, _index(5)),
        acks={1: _index(3)},
    )
    assert acks.CODEC.decode(acks.CODEC.encode(control)) == control
    intro = wire.Message(
        introduction=wire.Introduction("dave", b"\x04" * 136),
        numbers=((3, None),),
    )
    assert acks.CODEC.decode(acks.CODEC.encode(intro)) == intro
    text = _text().to_cbor()
    decoded = acks.CODEC.decode(acks.CODEC.encode(wire.Message(content=text)))
    assert decoded is not None and decoded.content is not None
    broken = models.GroupChatMessage(version=2, text="x", acks=b"\x01\x02")
    decoded = acks.CODEC.decode(broken.to_cbor())
    assert decoded is not None and decoded.acks == {}


# Through the network paths.


@pytest.mark.asyncio
async def test_a_confirmed_write_is_noted_for_the_state_machine(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_write_flow(fake_thinclient)
    group = Group.found(b"\x05" * 136, [b"\x05" * 136])
    async with persistent.asession() as sess:
        sess.add(persistent.GroupState(
            conversation_id=setup["conversation_id"], state=group_state.dump(group),
        ))
        await sess.commit()
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    await network.drain_mixwal_write_single(fake_thinclient, mw, {setup["bacap_stream"]})
    async with persistent.asession() as sess:
        (written,) = (await sess.exec(select(persistent.GroupWritten))).all()
    assert written.box_index == setup["first_message_index"]
    with Session(persistent._engine_sync) as sess:
        pwal = persistent.PlaintextWAL(
            id=written.pwal_id, bacap_stream=setup["bacap_stream"],
            conversation_id=setup["conversation_id"], bacap_payload=b"F",
        )
        persistent._record_sent_box(sess, b"\x00" * 104, pwal)
        assert len(sess.exec(select(persistent.GroupWritten)).all()) == 1


@pytest.mark.asyncio
async def test_a_confirmed_write_without_a_group_notes_nothing(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_write_flow(fake_thinclient)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    await network.drain_mixwal_write_single(fake_thinclient, mw, {setup["bacap_stream"]})
    async with persistent.asession() as sess:
        assert not (await sess.exec(select(persistent.GroupWritten))).all()


@pytest.mark.asyncio
async def test_a_box_read_through_the_network_is_told_in_order(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_read_flow(fake_thinclient)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw is not None and rcw.read_cap is not None
        stream = rcw.read_cap[:32]
        peer = (await sess.exec(select(persistent.ConversationPeer).where(
            persistent.ConversationPeer.read_cap_id == rcw.id
        ))).one()
        conv_id = peer.conversation.id
        own = secrets.token_bytes(136)
        group = Group.found(own, [own, rcw.read_cap])
        sess.add(persistent.GroupState(conversation_id=conv_id, state=group_state.dump(group)))
        await sess.commit()
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    await network.drain_mixwal_read_single(
        connection=fake_thinclient, rcw_read_cap=setup["read_cap"], mw=mw,
        draining_right_now={setup["bacap_stream"]},
    )
    async with persistent.asession() as sess:
        stored = await acks._load(sess, conv_id)
        assert stored is not None
    assert stored.group.readers[stream].last == setup["first_message_index"]
    assert stored.held == []


@pytest.mark.asyncio
async def test_the_gui_path_queues_and_wakes_the_listeners() -> None:
    chat, _ = await _with_bob()
    await chat.receive("bob", _text(), 3)
    await network.notify_outbound_text_sent(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=_text("hello"),
    )
    assert network.conversation_update_queue.get_nowait() == (chat.conversation_id, False)
    async with persistent.asession() as sess:
        (queued,) = (await sess.exec(select(persistent.PlaintextWAL))).all()
        row = await sess.get(persistent.GroupState, chat.conversation_id)
        assert row is not None
    assert _sent(queued).acks is not None
    assert [uuid.UUID(bytes=p) for p, _ in cbor2.loads(row.outgoing)] == [queued.id]


# Paths around the edges.


@pytest.mark.asyncio
async def test_an_induction_that_cannot_start_leaves_acknowledgements_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _with_bob()
    await chat.receive("bob", _text(), 3)

    async def payload(*_a: object, **_k: object) -> voucher._JoinerPayload:
        return voucher._JoinerPayload(uuid.uuid4(), b"w", b"p", b"i")

    async def never(conversation_id: int, *, deadline_s: float, epoch_s: float) -> bool:
        return False

    monkeypatch.setattr(voucher, "_read_joiner_payload", payload)
    monkeypatch.setattr(acks, "wait_for_outgoing", never)
    with pytest.raises(RuntimeError, match="still being sent"):
        await voucher.derive_read_and_induct(
            cast("ThinClient", object()), chat.conversation_id, "carol", b"v" * 32,
        )
    (sent,) = await chat.queue(_text())
    assert _sent(sent).acks is not None


async def _join(monkeypatch: pytest.MonkeyPatch, reply: models.GroupChatReplyWho) -> list[str]:
    newcomer = await _Chat().create()
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role="joiner", conversation_id=newcomer.conversation_id, step="awaiting",
            voucher=b"v" * 32, voucher_read_cap=b"\x09" * 136, box1_index=b"\x00" * 104,
            voucher_secret_key=b"k",
        ))
        await sess.commit()

    async def fake_read_box(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        return (b"sealed reply", b"\x00" * 104)

    monkeypatch.setattr(voucher, "_read_box", fake_read_box)

    class Opened:
        who_reply = reply.to_cbor()
        mutated_message_write_cap = secrets.token_bytes(168)

    class Connection:
        async def voucher_open(self, **_: object) -> Opened:
            return Opened()

    return await voucher.await_and_open(cast("ThinClient", Connection()), newcomer.conversation_id)


@pytest.mark.asyncio
async def test_a_joiner_skips_the_place_of_a_removed_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, _ = await _with_bob()
    await chat.add_peer("carol")
    await removal.remove_peer(conversation_id=chat.conversation_id, peer_id=chat.peers["bob"][0])
    reply = await _reply(chat)
    assert None in reply.please_adds
    assert sorted(await _join(monkeypatch, reply)) == ["me"]


@pytest.mark.asyncio
async def test_the_reply_leaves_out_a_member_no_longer_active() -> None:
    chat, bob = await _with_bob()
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, chat.peers["bob"][0])
        assert peer is not None
        peer.active = False
        sess.add(peer)
        await sess.commit()
    reply = await _reply(chat)
    assert reply.please_adds.count(None) == 1


@pytest.mark.asyncio
async def test_a_reply_that_cannot_be_read_costs_only_the_rosters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat, _ = await _with_bob()
    reply = await _reply(chat)
    newcomer = await _Chat().create()

    async def half_done(
        sess: persistent.AsyncSession, conv: persistent.Conversation,
        own: bytes, reply_who: models.GroupChatReplyWho,
    ) -> bool:
        sess.add(persistent.GroupState(conversation_id=conv.id, state=b"half"))
        await sess.flush()
        raise ValueError("rosters do not add up")

    monkeypatch.setattr(acks, "adopt", half_done)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, newcomer.conversation_id)
        assert conv is not None
        await voucher._adopt_rosters(sess, conv, newcomer.own_cap, reply)
        await sess.commit()
        assert await sess.get(persistent.GroupState, newcomer.conversation_id) is None
    assert "will not acknowledge" in caplog.text


@pytest.mark.asyncio
async def test_a_reply_with_the_wrong_size_of_read_cap_for_us_is_not_adopted() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, chat.conversation_id)
        assert conv is not None
        assert not await acks.adopt(sess, conv, chat.own_key, _alice_and_bob_reply())


async def _write_caps() -> int:
    async with persistent.asession() as sess:
        return len((await sess.exec(select(persistent.WriteCapWAL))).all())


@pytest.mark.asyncio
async def test_a_message_too_large_for_one_box_gets_its_stream() -> None:
    chat, _ = await _with_bob()
    before = await _write_caps()
    upload = await acks.append_outbound_text(
        conversation_id=chat.conversation_id,
        conversation_peer_id=chat.own_peer_id,
        gcm=_text("x" * 4000),
    )
    assert upload is not None
    assert await _write_caps() == before + 1


@pytest.mark.asyncio
async def test_a_headless_message_too_large_for_one_box_gets_its_stream() -> None:
    chat, _ = await _with_bob()
    before = await _write_caps()
    await _actions._queue_text(chat.conversation_id, _text("x" * 4000))
    assert await _write_caps() == before + 1


@pytest.mark.asyncio
@pytest.mark.real_sleeps
async def test_a_headless_message_waits_for_the_writer_lock() -> None:
    chat, _ = await _with_bob()
    async with persistent.conversation_log_order_lock(chat.conversation_id):
        queueing = asyncio.create_task(_actions._queue_text(chat.conversation_id, _text()))
        await asyncio.sleep(0.05)
        assert not queueing.done()
    await queueing
