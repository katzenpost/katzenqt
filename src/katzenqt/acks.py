"""Opportunistic acknowledgements and rosters, against the database.

Every decision is the group state machine's (:mod:`katzenqt.groupchat`):
this module drives it as a client that reads and writes for itself. It
keeps each conversation's machine as a ``GroupState`` row, tells it every
box read on a member's stream, in order, asks it what to send with each of
our messages, and tells it where each landed. See "Opportunistic
acknowledgements and backfill" in the group chat spec. It is free of Qt.

A conversation takes part only once it has a group state: one we started as
its founder, or one handed to us when we joined. A conversation from before
rosters existed has none, and neither sends nor reads acknowledgements.

The state is loaded, changed and saved in the caller's transaction, so
every change happens under the conversation's writer lock
(``persistent.conversation_log_order_lock``). The send path, which runs off
the event loop, only notes where a message landed (``GroupWritten``); the
next load takes it in.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, NamedTuple

import cbor2
from sqlmodel import col, select

from . import ack_codec, models, persistent
from .ack_protocol import REPLICA_EPOCH_SECONDS
from .groupchat import state as group_state
from .groupchat import wire
from .groupchat.events import EpochChanged, JoinReply
from .groupchat.group import Group, Outgoing

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1530

_inducting: set[int] = set()
"""Conversations an induction is under way for in this process. A state
saved mid-induction by a process that has since gone is released on load."""


def member_key(read_cap: bytes) -> bytes:
    """A member's identity: the public key its read cap begins with.

    >>> member_key(bytes(range(32)) + bytes(104)) == bytes(range(32))
    True
    """
    return read_cap[:32]


class GcmCodec:
    """Between a ``GroupChatMessage`` and what the state machine reads of
    it. Content is opaque to the machine: a message's own CBOR travels as
    its content and comes back out unchanged."""

    def decode(self, plaintext: bytes) -> wire.Message | None:
        """
        >>> codec = GcmCodec()
        >>> text = models.GroupChatMessage(version=2, text="hi")
        >>> codec.decode(text.to_cbor()).content is not None
        True
        >>> codec.decode(b"\\xff") is None
        True
        """
        try:
            gcm = models.GroupChatMessage.from_cbor(plaintext)
        except Exception:
            return None
        try:
            acks = ack_codec.decode(gcm.acks or b"")
        except ack_codec.AcksError:
            acks = {}
        intro = gcm.as_introduction
        control = gcm.msg_type in (
            models.GroupChatTypeEnum.CONTROL, models.GroupChatTypeEnum.INTRODUCTION,
        )
        return wire.Message(
            content=None if control else plaintext,
            introduction=(
                wire.Introduction(intro.display_name, intro.read_cap) if intro else None
            ),
            numbers=wire.decode_numbers(gcm.numbers or b""),
            introduced_at=wire.decode_introduced_at(gcm.introduced_at or b""),
            acks=acks,
        )

    def encode(self, message: wire.Message) -> bytes:
        return self.gcm(message).to_cbor()

    def gcm(self, message: wire.Message) -> models.GroupChatMessage:
        if message.content is not None:
            gcm = models.GroupChatMessage.from_cbor(message.content)
        elif message.introduction is not None:
            gcm = models.GroupChatMessage(
                version=models.GROUP_CHAT_VERSION,
                msg_type=models.GroupChatTypeEnum.INTRODUCTION,
                introduction=models.GroupChatPleaseAdd(
                    display_name=message.introduction.name,
                    read_cap=message.introduction.read_cap,
                ),
            )
        else:
            gcm = models.GroupChatMessage(
                version=models.GROUP_CHAT_VERSION, msg_type=models.GroupChatTypeEnum.CONTROL,
            )
        gcm.acks = ack_codec.encode(message.acks) or None
        gcm.numbers = wire.encode_numbers(message.numbers) or None
        gcm.introduced_at = (
            wire.encode_introduced_at(message.introduced_at)
            if message.introduced_at is not None
            else None
        )
        return gcm


CODEC = GcmCodec()


class _Stored(NamedTuple):
    row: persistent.GroupState
    group: Group
    held: list[dict[str, Any]]
    """Boxes read but not yet told: each a stream, a position, and once
    known its index, the next index, its plaintext and whether its content
    is still arriving through a substream."""
    outgoing: dict[uuid.UUID, int]
    """Which queued message, by its final PlaintextWAL, carries which
    token."""
    landed: list[persistent.GroupWritten]
    """Where our messages landed, taken in: deleted when the state is saved,
    so a load that is not saved takes them in again next time."""


def _replica_epoch() -> int:
    return int(time.time() // REPLICA_EPOCH_SECONDS)


async def _load(sess: "AsyncSession", conversation_id: int) -> _Stored | None:
    row = await sess.get(persistent.GroupState, conversation_id)
    if row is None:
        return None
    group = group_state.load(row.state, CODEC)
    outgoing = {uuid.UUID(bytes=pwal): token for pwal, token in cbor2.loads(row.outgoing)}
    landed = await sess.exec(
        select(persistent.GroupWritten).where(
            persistent.GroupWritten.conversation_id == conversation_id
        )
    )
    taken = list(landed.all())
    for written in taken:
        token = outgoing.pop(written.pwal_id, None)
        if token is not None:
            group.written(token, written.box_index)
    if group.ledger.holding and conversation_id not in _inducting:
        group.cancel_induction()
    epoch = _replica_epoch()
    if epoch > group.epoch:
        group.handle(EpochChanged(epoch))
    return _Stored(row, group, cbor2.loads(row.held), outgoing, taken)


async def _save(sess: "AsyncSession", stored: _Stored) -> None:
    stored.row.state = group_state.dump(stored.group)
    stored.row.held = cbor2.dumps(stored.held)
    stored.row.outgoing = cbor2.dumps(
        [[pwal.bytes, token] for pwal, token in stored.outgoing.items()]
    )
    sess.add(stored.row)
    for written in stored.landed:
        await sess.delete(written)
    stored.landed.clear()


async def _members(
    sess: "AsyncSession", conv: persistent.Conversation
) -> dict[bytes, tuple[persistent.ConversationPeer, persistent.ReadCapWAL]]:
    """The members of ``conv`` we read, by key: every active peer that is
    neither ourselves nor a transfer's substream."""
    rows = await sess.exec(
        select(persistent.ConversationPeer, persistent.ReadCapWAL)
        .join(
            persistent.ReadCapWAL,
            col(persistent.ReadCapWAL.id) == persistent.ConversationPeer.read_cap_id,
        )
        .join(
            persistent.ConversationPeerLink,
            col(persistent.ConversationPeerLink.conversation_peer_id)
            == persistent.ConversationPeer.id,
        )
        .where(persistent.ConversationPeerLink.conversation_id == conv.id)
    )
    members = {}
    for peer, rcw in rows:
        if peer.id == conv.own_peer_id or not peer.active:
            continue
        if peer.name.startswith(models.SUBSTREAM_NAME_PREFIX):
            continue
        if rcw.read_cap is not None:
            members[member_key(rcw.read_cap)] = (peer, rcw)
    return members


async def _founded(
    sess: "AsyncSession", conv: persistent.Conversation
) -> _Stored | None:
    """This conversation's group state, starting one first if we are its
    founder: alone in it, and never joined through a voucher."""
    stored = await _load(sess, conv.id)
    if stored is not None:
        return stored
    own_cap = await persistent.own_read_cap(sess, conv)
    if own_cap is None or conv.voucher_used or await _members(sess, conv):
        return None
    joining = await sess.exec(
        select(persistent.PendingVoucher).where(
            persistent.PendingVoucher.conversation_id == conv.id,
            persistent.PendingVoucher.role == "joiner",
        )
    )
    if joining.first() is not None:
        return None
    group = Group.found(own_cap, [own_cap], codec=CODEC)
    row = persistent.GroupState(conversation_id=conv.id, state=group_state.dump(group))
    return _Stored(row, group, [], {}, [])


async def _queue(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    stored: _Stored,
    outgoing: list[Outgoing],
) -> "tuple[list[uuid.UUID], list[models.SerializedRow]]":
    """Serialize what the state machine asks to send, one message to a box
    or substream, each written after the one before."""
    write_caps: list[uuid.UUID] = []
    entries: list[models.SerializedRow] = []
    for item in outgoing:
        send_op = models.SendOperation(
            bacap_stream=conv.write_cap, messages=[CODEC.gcm(item.message)]
        )
        new_write_caps, db_entries = await send_op.serialize_async(
            chunk_size=_CHUNK_SIZE, conversation_id=conv.id
        )
        last = db_entries[-1]
        if entries:
            assert isinstance(last, persistent.PlaintextWAL)
            last.after_id = entries[-1].id
        stored.outgoing[last.id] = item.token
        write_caps += new_write_caps
        entries += db_entries
    return write_caps, entries


def _store(
    sess: "AsyncSession",
    write_caps: list[uuid.UUID],
    entries: "list[models.SerializedRow]",
) -> None:
    for cap_uuid in write_caps:
        sess.add(persistent.WriteCapWAL(id=cap_uuid))
    for obj in entries:
        sess.add(obj)


async def serialize_with_acks(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    gcm: models.GroupChatMessage,
) -> "tuple[list[uuid.UUID], list[models.SerializedRow]]":
    """Serialize ``gcm`` for ``conv``'s own stream with the acknowledgements
    it carries, after any control message the state machine owes. The caller
    adds the rows to ``sess`` and commits, holding
    ``persistent.conversation_log_order_lock`` from before this call; the
    last row ends ``gcm``."""
    stored = await _founded(sess, conv)
    if stored is None:
        send_op = models.SendOperation(bacap_stream=conv.write_cap, messages=[gcm])
        return await send_op.serialize_async(chunk_size=_CHUNK_SIZE, conversation_id=conv.id)
    outgoing = stored.group.compose(gcm.to_cbor())
    queued = await _queue(sess, conv, stored, outgoing)
    await _save(sess, stored)
    return queued


async def serialize_introduction(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    gcm: models.GroupChatMessage,
) -> "tuple[list[uuid.UUID], list[models.SerializedRow]]":
    """Serialize ``gcm``, the ``Introduction`` that commits the induction
    under way, numbering the member it introduces. Called under the
    conversation's writer lock."""
    intro = gcm.as_introduction
    stored = await _load(sess, conv.id)
    if stored is None or intro is None:
        send_op = models.SendOperation(bacap_stream=conv.write_cap, messages=[gcm])
        return await send_op.serialize_async(chunk_size=_CHUNK_SIZE, conversation_id=conv.id)
    outgoing = stored.group.introduce_now(intro.display_name, intro.read_cap)
    queued = await _queue(sess, conv, stored, [outgoing])
    await _save(sess, stored)
    return queued


async def append_outbound_text(
    *,
    conversation_id: int,
    conversation_peer_id: int,
    gcm: models.GroupChatMessage,
) -> "persistent.OutboundUpload | None":
    """Queue one outbound chat message with its acknowledgements, and its
    ConversationLog entry, in one transaction under the conversation's
    writer lock. The text counterpart of
    ``persistent.append_outbound_chat``, with the same return: a description
    of the substream when the message was too large for one box."""
    async with persistent.conversation_log_order_lock(conversation_id):
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conversation_id)
            assert conv is not None
            new_write_caps, db_entries = await serialize_with_acks(sess, conv, gcm)
            _store(sess, new_write_caps, db_entries)
            upload = await persistent._outbound_upload_from_entries(
                sess, conversation_id, db_entries
            )
            sess.add(
                persistent.ConversationLog(
                    conversation_id=conversation_id,
                    conversation_peer_id=conversation_peer_id,
                    conversation_order=persistent.next_conversation_order(conversation_id),
                    payload=b"F" + gcm.to_cbor(),
                    network_status=1,
                    outgoing_pwal=db_entries[-1].id,
                )
            )
            await sess.commit()
            return upload


# Reading.


def _held_at(stored: _Stored, stream: bytes, at: int) -> dict[str, Any]:
    for box in stored.held:
        if box["stream"] == stream and box["at"] == at:
            return box
    box = {"stream": stream, "at": at, "index": None, "next": None,
           "plaintext": None, "waiting": False}
    stored.held.append(box)
    return box


def _release(stored: _Stored, stream: bytes) -> None:
    """Tell the state machine every box of ``stream`` that is ready, in
    order, up to the first still waiting."""
    boxes = sorted((b for b in stored.held if b["stream"] == stream), key=lambda b: b["at"])
    for box in boxes:
        if box["index"] is None or box["waiting"]:
            break
        stored.group.observe(stream, box["index"], box["plaintext"], box["next"])
        stored.held.remove(box)


async def on_message(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    position: int,
) -> None:
    """A message read at ``position`` on ``peer``'s stream. It is told to
    the state machine with its box, in order."""
    rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
    if rcw is None or rcw.read_cap is None:
        return
    stored = await _load(sess, peer.conversation.id)
    if stored is None:
        return
    stream = member_key(rcw.read_cap)
    _held_at(stored, stream, position)["plaintext"] = gcm.to_cbor()
    _release(stored, stream)
    await _save(sess, stored)


async def _fetching(sess: "AsyncSession", rcw_id: uuid.UUID, box_index: bytes) -> bool:
    """Whether the message whose indirection is the box at ``box_index`` is
    still on its way through a substream that has not failed."""
    piece = await sess.get(persistent.ReceivedPiece, (rcw_id, box_index[:8]))
    if piece is None or piece.chunk_type != b"I":
        return False
    substream = (
        await sess.exec(
            select(persistent.ReadCapWAL).where(
                persistent.ReadCapWAL.read_cap == piece.chunk[-136:]
            )
        )
    ).first()
    return substream is not None and substream.substream_failure is None


async def box_read(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    rcw: persistent.ReadCapWAL,
    box_index: bytes,
) -> None:
    """A box that ends a message, or announces one through a substream, was
    read on ``peer``'s own stream; ``rcw.next_index`` already follows it."""
    if rcw.read_cap is None or rcw.next_index is None:
        return
    stored = await _load(sess, peer.conversation.id)
    if stored is None:
        return
    stream = member_key(rcw.read_cap)
    box = _held_at(stored, stream, persistent.box_position(box_index))
    box.update(index=box_index, next=rcw.next_index,
               waiting=await _fetching(sess, rcw.id, box_index))
    _release(stored, stream)
    await _save(sess, stored)


async def settle(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    rcw: persistent.ReadCapWAL,
) -> None:
    """A substream of ``peer``'s stream has finished, one way or another:
    whatever waited on it can be told."""
    if rcw.read_cap is None:
        return
    stored = await _load(sess, peer.conversation.id)
    if stored is None:
        return
    stream = member_key(rcw.read_cap)
    for box in stored.held:
        if box["stream"] == stream and box["waiting"]:
            box["waiting"] = await _fetching(sess, rcw.id, box["index"])
    _release(stored, stream)
    await _save(sess, stored)


# Writing.


async def forget_outgoing(
    sess: "AsyncSession", conversation_id: int, pwal_id: uuid.UUID
) -> None:
    """The message ending in ``pwal_id`` will never be written: what it
    acknowledged is owed again. Called under the conversation's writer
    lock."""
    stored = await _load(sess, conversation_id)
    if stored is None or pwal_id not in stored.outgoing:
        return
    stored.group.abandoned(stored.outgoing.pop(pwal_id))
    await _save(sess, stored)


async def _abandoned(sess: "AsyncSession", pwal_id: uuid.UUID) -> bool:
    """Whether the message ending in ``pwal_id`` was dropped unwritten: it
    is neither queued nor sent. Asked in that order, since a message leaves
    the queue in the transaction that records it as sent."""
    if await sess.get(persistent.PlaintextWAL, pwal_id) is not None:
        return False
    return await sess.get(persistent.SentLog, pwal_id) is None


async def wait_for_outgoing(conversation_id: int, *, deadline_s: float) -> bool:
    """Wait until every message the state machine is waiting on has been
    written: until then a reply to a new member cannot say what our roster
    will be."""
    async with persistent.conversation_log_order_lock(conversation_id):
        async with persistent.asession() as sess:
            stored = await _load(sess, conversation_id)
            if stored is None:
                return True
            owed = []
            for pwal_id in list(stored.outgoing):
                if await _abandoned(sess, pwal_id):
                    stored.group.abandoned(stored.outgoing.pop(pwal_id))
                else:
                    owed.append(pwal_id)
            await _save(sess, stored)
            await sess.commit()
    for pwal_id in owed:
        if not await persistent.wait_for_sent(pwal_id, deadline_s=deadline_s):
            return False
    return True


# Inducting.


@asynccontextmanager
async def held_back(conversation_id: int) -> AsyncIterator[None]:
    """Hold this conversation's acknowledgements and numbering back while a
    new member is inducted, starting it on each stream where we have read
    it now. Anything owed is queued first: wait for it with
    ``wait_for_outgoing`` before ``hand_over``."""
    _inducting.add(conversation_id)
    try:
        async with persistent.conversation_log_order_lock(conversation_id):
            async with persistent.asession() as sess:
                conv = await sess.get(persistent.Conversation, conversation_id)
                assert conv is not None
                stored = await _founded(sess, conv)
                if stored is not None:
                    outgoing = stored.group.begin_induction()
                    _store(sess, *await _queue(sess, conv, stored, outgoing))
                    await _save(sess, stored)
                    await sess.commit()
        yield
    finally:
        _inducting.discard(conversation_id)
        async with persistent.conversation_log_order_lock(conversation_id):
            async with persistent.asession() as sess:
                stored = await _load(sess, conversation_id)
                if stored is not None:
                    await _save(sess, stored)
                    await sess.commit()


async def hand_over(
    sess: "AsyncSession", conv: persistent.Conversation
) -> models.GroupChatReplyWho | None:
    """The reply to a new member, for a conversation that keeps rosters:
    our roster in order, each member's read cap starting where the new
    member is to start reading, and what it needs to follow every roster
    from here on. None when this conversation keeps no rosters."""
    stored = await _founded(sess, conv)
    wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
    if stored is None or wcw is None or wcw.next_index is None:
        return None
    if stored.row not in sess:
        sess.add(stored.row)
    reply = stored.group.reply(wcw.next_index)
    members = await _members(sess, conv)
    own_peer = await sess.get(persistent.ConversationPeer, conv.own_peer_id)
    please_adds: list[models.GroupChatPleaseAdd | None] = []
    for member in reply.members:
        if member is None:
            please_adds.append(None)
            continue
        read_cap, start = member
        key = member_key(read_cap)
        if key == stored.group.me:
            name = own_peer.name if own_peer is not None else "?"
        elif key in members:
            name = members[key][0].name
        else:
            please_adds.append(None)
            continue
        please_adds.append(models.GroupChatPleaseAdd(display_name=name, read_cap=key + start))
    return models.GroupChatReplyWho(
        please_adds=please_adds,
        rosters=[list(roster) if roster is not None else None for roster in reply.rosters],
        introductions=list(reply.introductions),
        introducer=reply.introducer,
        newcomer=reply.newcomer,
    )


def _valid_position(position: int) -> bool:
    return 0 <= position < 2**64


def _join_reply(reply: models.GroupChatReplyWho) -> JoinReply | None:
    """The reply as the state machine takes it, or None if it carries no
    rosters or cannot be read."""
    places = len(reply.please_adds)
    if (
        reply.rosters is None
        or reply.introducer is None
        or reply.newcomer != places
        or not reply.introducer < places < wire.ROSTER_MAX
        or reply.please_adds[reply.introducer] is None
        or len(reply.rosters) != places
    ):
        return None
    keys = [member_key(p.read_cap) for p in reply.please_adds if p is not None]
    if len(set(keys)) != len(keys):
        return None
    for roster in reply.rosters:
        for index, slot, at in roster or ():
            if not 0 <= index < wire.ROSTER_MAX or not (slot is None or 0 <= slot < places):
                return None
            if not (at == -1 or _valid_position(at)):
                return None
    introductions = tuple(
        (by, at, member)
        for by, at, member in reply.introductions or ()
        if 0 <= by < places and 0 <= member < places and _valid_position(at)
    )
    return JoinReply(
        members=tuple(
            (p.read_cap, p.read_cap[32:]) if p is not None else None for p in reply.please_adds
        ),
        rosters=tuple(tuple(r) if r is not None else None for r in reply.rosters),
        introductions=introductions,
        introducer=reply.introducer,
        newcomer=reply.newcomer,
    )


async def adopt(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    own_read_cap: bytes,
    reply: models.GroupChatReplyWho,
) -> bool:
    """Start this conversation's group state from the reply that let us in.
    False, and nothing stored, when the reply carries no rosters or cannot
    be read."""
    handed = _join_reply(reply)
    if handed is None or len(own_read_cap) != 136 or member_key(own_read_cap) in {
        member_key(m[0]) for m in handed.members if m
    }:
        return False
    group = Group.joined(own_read_cap, handed, codec=CODEC)
    row = await sess.get(persistent.GroupState, conv.id)
    if row is None:
        row = persistent.GroupState(conversation_id=conv.id, state=b"")
    await _save(sess, _Stored(row, group, [], {}, []))
    await sess.flush()
    return True


# Membership.


async def forget_member(
    sess: "AsyncSession", conv: persistent.Conversation, read_cap: bytes
) -> None:
    """A member is being removed: stop following it, keeping the places it
    holds in every roster. Called under the conversation's writer lock."""
    stored = await _load(sess, conv.id)
    if stored is None:
        return
    stored.group.remove(member_key(read_cap))
    stored.held[:] = [b for b in stored.held if b["stream"] != member_key(read_cap)]
    await _save(sess, stored)


async def summary(
    sess: "AsyncSession", conv: persistent.Conversation
) -> "tuple[list[str] | None, dict[str, int]]":
    """For inspection: our roster as display names in order, with "?" where
    a member is not known by name, and how far each member has acknowledged
    our stream. The roster is None for a conversation that keeps none."""
    stored = await _load(sess, conv.id)
    if stored is None:
        return None, {}
    members = await _members(sess, conv)
    own_peer = await sess.get(persistent.ConversationPeer, conv.own_peer_id)
    names = []
    for key in stored.group.rosters.known(stored.group.me).values():
        if key == stored.group.me:
            names.append(own_peer.name if own_peer is not None else "?")
        else:
            names.append(members[key][0].name if key in members else "?")
    acked = {
        members[key][0].name: at
        for key, at in stored.group.ledger.acked_by.items()
        if key in members
    }
    return names, acked


__all__ = [
    "CODEC",
    "GcmCodec",
    "adopt",
    "append_outbound_text",
    "box_read",
    "forget_member",
    "forget_outgoing",
    "hand_over",
    "held_back",
    "member_key",
    "on_message",
    "serialize_introduction",
    "serialize_with_acks",
    "settle",
    "summary",
    "wait_for_outgoing",
]
