"""Opportunistic acknowledgements and rosters, against the database.

The rules are in :mod:`katzenqt.rosters` and :mod:`katzenqt.ack_codec`,
which are pure. This module loads the facts they work from, applies what
they decide, and is called from the send path, the read path and the
voucher handshake. See "Opportunistic acknowledgements and backfill" in
the group chat protocol spec. It is free of Qt.

A conversation takes part only once it has a roster of its own: one we
started as its founder, or one handed to us when we joined. A conversation
from before rosters existed has none, and neither sends nor reads
acknowledgements.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import TYPE_CHECKING, NamedTuple

import cbor2
from sqlmodel import col, select

from . import ack_codec, models, persistent, rosters

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1530

inducting: set[int] = set()
"""Conversations for which a reply to a new member has been built and its
``Introduction`` is not yet queued. No message of ours may number anyone in
between, or the place the reply promised the new member would be taken."""


def member_key(read_cap: bytes) -> bytes:
    """A member's identity: the public key its read cap begins with.

    >>> member_key(bytes(range(32)) + bytes(104)) == bytes(range(32))
    True
    """
    return read_cap[:32]


async def own_key(
    sess: "AsyncSession", conv: persistent.Conversation
) -> bytes | None:
    cap = await persistent.own_read_cap(sess, conv)
    return None if cap is None else member_key(cap)


async def load_group(
    sess: "AsyncSession", conversation_id: int
) -> rosters.Group:
    """Everything stored about this conversation's rosters."""
    group = rosters.Group()
    members = await sess.exec(
        select(persistent.RosterMember).where(
            persistent.RosterMember.conversation_id == conversation_id
        )
    )
    for member in members:
        group.seen[member.member_key] = persistent.position_int(member.seen)
        if member.base_roster is not None:
            group.bases[member.member_key] = tuple(
                cbor2.loads(member.base_roster)
            )
        elif member.base_introducer is not None:
            group.bases[member.member_key] = rosters.Inherited(
                member.base_introducer,
                persistent.position_int(member.base_position),
            )
    introductions = await sess.exec(
        select(persistent.IntroductionSeen).where(
            persistent.IntroductionSeen.conversation_id == conversation_id,
            col(persistent.IntroductionSeen.position).is_not(None),
        )
    )
    for seen in introductions:
        at = (seen.introducer_key, persistent.position_int(seen.position))
        group.introductions[at] = seen.member_key
    levels: dict[tuple[bytes, int], dict[int, int]] = {}
    acked = await sess.exec(
        select(persistent.AckLevel).where(
            persistent.AckLevel.conversation_id == conversation_id
        )
    )
    for level in acked:
        at = (level.acker_key, persistent.position_int(level.position))
        levels.setdefault(at, {})[level.roster_index] = (
            persistent.position_int(level.reached)
        )
    group.acks.update(levels)
    return group


async def _roster_row(
    sess: "AsyncSession", conversation_id: int, key: bytes
) -> persistent.RosterMember | None:
    return (
        await sess.exec(
            select(persistent.RosterMember).where(
                persistent.RosterMember.conversation_id == conversation_id,
                persistent.RosterMember.member_key == key,
            )
        )
    ).first()


async def _other_members(
    sess: "AsyncSession", conv: persistent.Conversation
) -> dict[bytes, tuple[persistent.ConversationPeer, persistent.ReadCapWAL]]:
    """The members of ``conv`` we read, by key: every active peer that is
    neither ourselves nor a transfer's substream."""
    rows = await sess.exec(
        select(persistent.ConversationPeer, persistent.ReadCapWAL)
        .join(
            persistent.ReadCapWAL,
            col(persistent.ReadCapWAL.id)
            == persistent.ConversationPeer.read_cap_id,
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


async def enabled(
    sess: "AsyncSession", conv: persistent.Conversation
) -> bytes | None:
    """Our own key if this conversation has rosters, else None."""
    key = await own_key(sess, conv)
    if key is None or await _roster_row(sess, conv.id, key) is None:
        return None
    return key


async def ensure_own_roster(
    sess: "AsyncSession", conv: persistent.Conversation
) -> bytes | None:
    """Our own key if this conversation has rosters, starting one first if
    we are its founder: alone in it, and never joined through a voucher."""
    key = await own_key(sess, conv)
    if key is None:
        return None
    if await _roster_row(sess, conv.id, key) is not None:
        return key
    if conv.voucher_used or await _other_members(sess, conv):
        return None
    joining = await sess.exec(
        select(persistent.PendingVoucher).where(
            persistent.PendingVoucher.conversation_id == conv.id,
            persistent.PendingVoucher.role == "joiner",
        )
    )
    if joining.first() is not None:
        return None
    sess.add(
        persistent.RosterMember(
            conversation_id=conv.id,
            member_key=key,
            base_roster=cbor2.dumps([key]),
        )
    )
    await sess.flush()
    return key


async def _blocked(
    sess: "AsyncSession", conv: persistent.Conversation, key: bytes
) -> bool:
    """Whether acknowledgements must wait: we are introducing a new member
    and its ``Introduction`` has not been written yet."""
    if conv.id in inducting:
        return True
    introducing = await sess.exec(
        select(persistent.PendingVoucher).where(
            persistent.PendingVoucher.conversation_id == conv.id,
            persistent.PendingVoucher.role == "inductor",
        )
    )
    if introducing.first() is not None:
        return True
    unwritten = await sess.exec(
        select(persistent.IntroductionSeen).where(
            persistent.IntroductionSeen.conversation_id == conv.id,
            persistent.IntroductionSeen.introducer_key == key,
            col(persistent.IntroductionSeen.position).is_(None),
        )
    )
    return unwritten.first() is not None


class _Attached(NamedTuple):
    key: bytes
    levels: dict[int, int]


async def _attach(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    gcm: models.GroupChatMessage,
) -> _Attached | None:
    """Give ``gcm`` every acknowledgement pending in ``conv``: for each
    member we have numbered, the furthest box newly read on its stream."""
    if gcm.as_introduction is not None or gcm.file_upload is not None:
        return None
    key = await ensure_own_roster(sess, conv)
    if key is None or await _blocked(sess, conv, key):
        return None
    roster = rosters.roster_of(await load_group(sess, conv.id), key)
    if roster is None:
        return None
    acks: dict[int, bytes] = {}
    for other, (_, rcw) in (await _other_members(sess, conv)).items():
        if (
            rcw.last_read_index is None
            or rcw.last_read_index == rcw.acked_index
        ):
            continue
        if other not in roster:
            continue
        acks[roster.index(other)] = rcw.last_read_index
        rcw.acked_index = rcw.last_read_index
        sess.add(rcw)
    if not acks:
        return None
    gcm.acks = ack_codec.encode(acks)
    return _Attached(
        key,
        {
            index: persistent.box_position(value)
            for index, value in acks.items()
        },
    )


async def serialize_with_acks(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    gcm: models.GroupChatMessage,
) -> "tuple[list[uuid.UUID], list[models.SerializedRow]]":
    """Attach the pending acknowledgements to ``gcm`` and serialize it for
    ``conv``'s own stream. The caller adds the rows to ``sess`` and commits:
    what was acknowledged is recorded in that same transaction, so a message
    that is never queued acknowledges nothing."""
    attached = await _attach(sess, conv, gcm)
    send_op = models.SendOperation(
        bacap_stream=conv.write_cap, messages=[gcm]
    )
    new_write_caps, db_entries = await send_op.serialize_async(
        chunk_size=_CHUNK_SIZE, conversation_id=conv.id
    )
    if attached is not None:
        logger.debug(
            "acknowledging %d member(s) in conversation %d",
            len(attached.levels),
            conv.id,
        )
        sess.add(
            persistent.OutgoingAcks(
                pwal_id=db_entries[-1].id,
                conversation_id=conv.id,
                acker_key=attached.key,
                levels=cbor2.dumps(attached.levels),
            )
        )
    return new_write_caps, db_entries


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
            new_write_caps, db_entries = await serialize_with_acks(
                sess, conv, gcm
            )
            for cap_uuid in new_write_caps:
                sess.add(persistent.WriteCapWAL(id=cap_uuid))
            for obj in db_entries:
                sess.add(obj)
            upload = await persistent._outbound_upload_from_entries(
                sess, conversation_id, db_entries
            )
            sess.add(
                persistent.ConversationLog(
                    conversation_id=conversation_id,
                    conversation_peer_id=conversation_peer_id,
                    conversation_order=persistent.next_conversation_order(
                        conversation_id
                    ),
                    payload=b"F" + gcm.to_cbor(),
                    network_status=1,
                    outgoing_pwal=db_entries[-1].id,
                )
            )
            await sess.commit()
            return upload


async def on_message(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    position: int,
) -> None:
    """Record what a message read at ``position`` on ``peer``'s stream says
    about rosters: whom it acknowledges, and whom it introduces."""
    conv = peer.conversation
    own = await enabled(sess, conv)
    rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
    if own is None or rcw is None or rcw.read_cap is None:
        return
    sender = member_key(rcw.read_cap)
    at = persistent.position_bytes(position)
    if gcm.acks:
        try:
            named = ack_codec.decode(gcm.acks)
        except ack_codec.AcksError as error:
            logger.warning(
                "ignoring malformed acks from %r in conversation %d: %s",
                peer.name,
                conv.id,
                error,
            )
            named = {}
        for index, value in named.items():
            await sess.merge(
                persistent.AckLevel(
                    conversation_id=conv.id,
                    acker_key=sender,
                    position=at,
                    roster_index=index,
                    reached=persistent.position_bytes(
                        persistent.box_position(value)
                    ),
                    value=value,
                )
            )
    if (intro := gcm.as_introduction) is not None:
        await _record_introduction(
            sess, conv.id, own, sender, at, member_key(intro.read_cap)
        )


async def _record_introduction(
    sess: "AsyncSession",
    conversation_id: int,
    own: bytes,
    introducer: bytes,
    at: bytes,
    member: bytes,
) -> None:
    known = await sess.exec(
        select(persistent.IntroductionSeen).where(
            persistent.IntroductionSeen.conversation_id == conversation_id,
            persistent.IntroductionSeen.introducer_key == introducer,
            persistent.IntroductionSeen.position == at,
        )
    )
    if known.first() is None:
        sess.add(
            persistent.IntroductionSeen(
                conversation_id=conversation_id,
                introducer_key=introducer,
                position=at,
                member_key=member,
            )
        )
    if (
        member != own
        and await _roster_row(sess, conversation_id, member) is None
    ):
        sess.add(
            persistent.RosterMember(
                conversation_id=conversation_id,
                member_key=member,
                base_introducer=introducer,
                base_position=at,
            )
        )
    await sess.flush()


async def box_read(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    rcw: persistent.ReadCapWAL,
    box_index: bytes,
) -> None:
    """A box that ends a message was read on ``peer``'s own stream."""
    rcw.frontier_index = box_index
    await settle(sess, peer, rcw)


async def settle(
    sess: "AsyncSession",
    peer: persistent.ConversationPeer,
    rcw: persistent.ReadCapWAL,
) -> None:
    """Bring forward how far ``peer``'s stream may be acknowledged: to the
    last message read, unless an earlier one is still being fetched through
    a substream. Then take any acknowledgement that has become readable."""
    if rcw.frontier_index is None or await _fetching(sess, rcw.id):
        return
    rcw.last_read_index = rcw.frontier_index
    sess.add(rcw)
    conv = peer.conversation
    own = await enabled(sess, conv)
    if own is None or rcw.read_cap is None:
        return
    member = await _roster_row(sess, conv.id, member_key(rcw.read_cap))
    if member is not None:
        reached = persistent.box_position(rcw.frontier_index)
        if reached > persistent.position_int(member.seen):
            member.seen = persistent.position_bytes(reached)
            sess.add(member)
    await sess.flush()
    await claim(sess, conv, own)


async def _fetching(sess: "AsyncSession", rcw_id: uuid.UUID) -> bool:
    """Whether a message announced on this stream is still on its way
    through a substream that has not failed."""
    pieces = await sess.exec(
        select(persistent.ReceivedPiece).where(
            persistent.ReceivedPiece.read_cap == rcw_id,
            persistent.ReceivedPiece.chunk_type == b"I",
        )
    )
    for piece in pieces:
        substream = (
            await sess.exec(
                select(persistent.ReadCapWAL).where(
                    persistent.ReadCapWAL.read_cap == piece.chunk[-136:]
                )
            )
        ).first()
        if substream is not None and substream.substream_failure is None:
            return True
    return False


async def claim(
    sess: "AsyncSession", conv: persistent.Conversation, own: bytes
) -> None:
    """Read every acknowledgement not yet placed. One that names us is
    checked against the boxes we wrote, and raises how far its sender is
    known to have read our stream."""
    waiting = (
        await sess.exec(
            select(persistent.AckLevel).where(
                persistent.AckLevel.conversation_id == conv.id,
                col(persistent.AckLevel.value).is_not(None),
            )
        )
    ).all()
    if not waiting:
        return
    group = await load_group(sess, conv.id)
    members = await _other_members(sess, conv)
    written = persistent.position_int(
        (
            await _roster_row(sess, conv.id, own) or persistent.RosterMember()
        ).seen
    )
    for level in waiting:
        roster = rosters.roster_of(group, level.acker_key)
        if roster is None or level.roster_index >= len(roster):
            continue
        assert level.value is not None
        if roster[level.roster_index] != own:
            level.value = None
        elif await _accept(
            sess, conv, members.get(level.acker_key), level.value
        ):
            level.value = None
        elif persistent.position_int(level.reached) <= written:
            # Names a position we have written, with an index we did not:
            # stale, forged or misnumbered. Later ones may yet be ours.
            level.value = None
        sess.add(level)


async def _accept(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    acker: "tuple[persistent.ConversationPeer, persistent.ReadCapWAL] | None",
    value: bytes,
) -> bool:
    """The Sent-box check: ``value`` must be the index of a box we wrote."""
    box = (
        await sess.exec(
            select(persistent.SentBox).where(
                persistent.SentBox.bacap_stream == conv.write_cap,
                persistent.SentBox.box_index == value,
            )
        )
    ).first()
    if box is None or acker is None:
        return False
    peer = acker[0]
    if persistent.position_int(box.position) > persistent.position_int(
        peer.acked_position
    ):
        peer.acked_position = box.position
        sess.add(peer)
        logger.info(
            "ACKED conversation=%d peer=%s position=%d",
            conv.id,
            peer.name,
            persistent.position_int(box.position),
        )
    return True


async def wait_for_outgoing(
    conversation_id: int, *, deadline_s: float
) -> bool:
    """Wait until every queued message of ours that carries acknowledgements
    has been written. Until then our roster may still grow by them, and a
    reply to a new member cannot say where its roster index will be."""
    async with persistent.asession() as sess:
        owed = (
            await sess.exec(
                select(persistent.OutgoingAcks.pwal_id).where(
                    persistent.OutgoingAcks.conversation_id == conversation_id
                )
            )
        ).all()
    for pwal_id in owed:
        if not await persistent.wait_for_sent(pwal_id, deadline_s=deadline_s):
            return False
    return True


async def hand_over(
    sess: "AsyncSession", conv: persistent.Conversation
) -> models.GroupChatReplyWho | None:
    """The reply to a new member, for a conversation that keeps rosters:
    our roster in order, then anyone we have read of but not yet numbered,
    with what the new member needs to follow every roster from here on.
    None when this conversation keeps no rosters."""
    own = await ensure_own_roster(sess, conv)
    own_cap = await persistent.own_read_cap(sess, conv)
    if own is None or own_cap is None:
        return None
    group = await load_group(sess, conv.id)
    roster = rosters.roster_of(group, own)
    if roster is None:
        return None
    members = await _other_members(sess, conv)
    unnumbered = sorted(
        (key for key in members if key not in roster),
        key=lambda key: members[key][0].id,
    )
    known = [*roster, *unnumbered]
    please_adds: list[models.GroupChatPleaseAdd | None] = []
    for key in known:
        if key == own:
            please_adds.append(
                models.GroupChatPleaseAdd(
                    display_name=conv.own_peer.name, read_cap=own_cap
                )
            )
        elif key in members and members[key][1].read_cap is not None:
            peer, rcw = members[key]
            assert rcw.read_cap is not None
            please_adds.append(
                models.GroupChatPleaseAdd(
                    display_name=peer.name, read_cap=rcw.read_cap
                )
            )
        else:
            please_adds.append(None)
    handed = rosters.hand_over(group, known)
    return models.GroupChatReplyWho(
        please_adds=please_adds,
        roster_size=len(roster),
        rosters=handed.rosters,
        seen=handed.seen,
        introductions=handed.introductions,
        unsettled=handed.unsettled,
    )


def _valid_position(position: int) -> bool:
    return 0 <= position < 2**64


async def adopt(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    own: bytes,
    reply: models.GroupChatReplyWho,
) -> bool:
    """Start this conversation's rosters from the reply that let us in: our
    own roster is the introducer's with ourselves after it, and every other
    member's is as the introducer handed it over. False, and nothing stored,
    when the reply carries no rosters or cannot be read."""
    size = reply.roster_size
    places = len(reply.please_adds)
    if (
        size is None
        or reply.rosters is None
        or reply.seen is None
        or not size <= places <= rosters.ROSTER_MAX
        or len(reply.rosters) != places
        or len(reply.seen) != places
    ):
        return False
    known = [
        rosters.placeholder(place)
        if entry is None
        else member_key(entry.read_cap)
        for place, entry in enumerate(reply.please_adds)
    ]
    if own in known or len(set(known)) != places:
        return False
    group = rosters.adopt(
        known,
        rosters.Handover(
            rosters=reply.rosters,
            seen=reply.seen,
            introductions=[
                (introducer, position, member)
                for introducer, position, member in reply.introductions or ()
                if _valid_position(position)
            ],
            unsettled=[
                (sender, position, levels)
                for sender, position, levels in reply.unsettled or ()
                if _valid_position(position)
                and all(
                    0 <= index < rosters.ROSTER_MAX
                    and _valid_position(reached)
                    for index, reached in levels.items()
                )
            ],
        ),
    )
    for table in (
        persistent.RosterMember,
        persistent.IntroductionSeen,
        persistent.AckLevel,
    ):
        for row in await sess.exec(
            select(table).where(table.conversation_id == conv.id)
        ):
            await sess.delete(row)
    await sess.flush()
    sess.add(
        persistent.RosterMember(
            conversation_id=conv.id,
            member_key=own,
            base_roster=cbor2.dumps([*known[:size], own]),
        )
    )
    for key in known:
        base = group.bases.get(key)
        seen = group.seen.get(key, -1)
        inherited = base if isinstance(base, rosters.Inherited) else None
        sess.add(
            persistent.RosterMember(
                conversation_id=conv.id,
                member_key=key,
                seen=persistent.position_bytes(seen)
                if _valid_position(seen)
                else None,
                base_roster=(
                    cbor2.dumps(list(base))
                    if isinstance(base, tuple)
                    else None
                ),
                base_introducer=inherited.introducer if inherited else None,
                base_position=(
                    persistent.position_bytes(inherited.position)
                    if inherited
                    else None
                ),
            )
        )
    for (introducer, position), member in group.introductions.items():
        sess.add(
            persistent.IntroductionSeen(
                conversation_id=conv.id,
                introducer_key=introducer,
                position=persistent.position_bytes(position),
                member_key=member,
            )
        )
    for (sender, position), levels in group.acks.items():
        for index, reached in levels.items():
            sess.add(
                persistent.AckLevel(
                    conversation_id=conv.id,
                    acker_key=sender,
                    position=persistent.position_bytes(position),
                    roster_index=index,
                    reached=persistent.position_bytes(reached),
                )
            )
    await sess.flush()
    return True


async def introduced(
    sess: "AsyncSession",
    conv: persistent.Conversation,
    read_cap: bytes,
    pending_pwal: uuid.UUID,
) -> None:
    """We have queued an ``Introduction`` of the member read by
    ``read_cap``. It takes the next place in our roster once that message is
    written, and its own roster starts as ours with itself after it."""
    own = await enabled(sess, conv)
    if own is None:
        return
    member = member_key(read_cap)
    roster = rosters.roster_of(await load_group(sess, conv.id), own)
    if roster is None or member in roster:
        return
    sess.add(
        persistent.IntroductionSeen(
            conversation_id=conv.id,
            introducer_key=own,
            member_key=member,
            pending_pwal=pending_pwal,
        )
    )
    if await _roster_row(sess, conv.id, member) is None:
        sess.add(
            persistent.RosterMember(
                conversation_id=conv.id,
                member_key=member,
                base_roster=cbor2.dumps([*roster, member]),
            )
        )
    await sess.flush()


async def forget_member(
    sess: "AsyncSession", conv: persistent.Conversation, read_cap: bytes
) -> None:
    """A member is being removed. Every roster still counts from the place
    it held, so the place is kept and only the key is forgotten: wherever
    the key is stored it is replaced by a placeholder."""
    own = await enabled(sess, conv)
    if own is None:
        return
    key = member_key(read_cap)
    roster = rosters.roster_of(await load_group(sess, conv.id), own)
    if roster is not None and key in roster:
        token = rosters.placeholder(roster.index(key))
    else:
        token = b"retired:" + secrets.token_bytes(8)
    members = await sess.exec(
        select(persistent.RosterMember).where(
            persistent.RosterMember.conversation_id == conv.id
        )
    )
    for row in members.all():
        base = row.base_roster
        if base is not None and key in (entries := cbor2.loads(base)):
            base = cbor2.dumps([token if e == key else e for e in entries])
        introducer = (
            token if row.base_introducer == key else row.base_introducer
        )
        if row.member_key != key:
            row.base_roster, row.base_introducer = base, introducer
            sess.add(row)
            continue
        await sess.delete(row)
        await sess.flush()
        sess.add(
            persistent.RosterMember(
                conversation_id=conv.id,
                member_key=token,
                seen=row.seen,
                base_roster=base,
                base_introducer=introducer,
                base_position=row.base_position,
            )
        )
    introductions = await sess.exec(
        select(persistent.IntroductionSeen).where(
            persistent.IntroductionSeen.conversation_id == conv.id
        )
    )
    for seen in introductions.all():
        if key in (seen.introducer_key, seen.member_key):
            if seen.introducer_key == key:
                seen.introducer_key = token
            if seen.member_key == key:
                seen.member_key = token
            sess.add(seen)
    levels = await sess.exec(
        select(persistent.AckLevel).where(
            persistent.AckLevel.conversation_id == conv.id,
            persistent.AckLevel.acker_key == key,
        )
    )
    for level in levels.all():
        await sess.delete(level)
        await sess.flush()
        sess.add(
            persistent.AckLevel(
                conversation_id=conv.id,
                acker_key=token,
                position=level.position,
                roster_index=level.roster_index,
                reached=level.reached,
            )
        )
    await sess.flush()


async def summary(
    sess: "AsyncSession", conv: persistent.Conversation
) -> "tuple[list[str] | None, dict[str, int]]":
    """For inspection: our roster as display names in order, with "?" where
    a member is no longer known, and how far each member has acknowledged
    our stream. The roster is None for a conversation that keeps none."""
    members = await _other_members(sess, conv)
    acked = {
        peer.name: persistent.position_int(peer.acked_position)
        for peer, _ in members.values()
        if peer.acked_position is not None
    }
    own = await enabled(sess, conv)
    if own is None:
        return None, acked
    roster = rosters.roster_of(await load_group(sess, conv.id), own) or ()
    own_peer = await sess.get(persistent.ConversationPeer, conv.own_peer_id)
    names = []
    for key in roster:
        if key == own:
            names.append(own_peer.name if own_peer is not None else "?")
        else:
            names.append(members[key][0].name if key in members else "?")
    return names, acked


__all__ = [
    "adopt",
    "append_outbound_text",
    "box_read",
    "claim",
    "enabled",
    "ensure_own_roster",
    "forget_member",
    "hand_over",
    "inducting",
    "introduced",
    "load_group",
    "member_key",
    "on_message",
    "own_key",
    "serialize_with_acks",
    "settle",
    "summary",
    "wait_for_outgoing",
]
