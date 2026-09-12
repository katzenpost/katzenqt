"""Route an assembled group-chat message by its explicit type.

The network receive path reassembles a :class:`katzenqt.models.GroupChatMessage`
and hands it here. A small registry keyed by ``msg_type`` decides what becomes
of it: ordinary chat messages are appended to the conversation log, as before;
tally messages are diverted to the tally controller and never touch the log, so
they do not surface as empty chat lines.

This module is free of Qt; the receive path that calls it must stay so too.
"""
from __future__ import annotations

import logging

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import models, persistent
from .models import GroupChatPleaseAdd, GroupChatTypeEnum
from .tally import controller as tally_controller

logger = logging.getLogger(__name__)


async def dispatch(sess, peer, gcm, full_payload) -> "tuple[bool, bool, tuple[int, str] | None]":
    """Handle ``gcm`` for ``peer``. Returns ``(convlog_added, signal_send,
    peer_added)``: whether a ConversationLog row was added (so the chat view
    is notified), whether outbound work was staged that the send loop must
    be poked for, and, if a newcomer peer was added, their
    ``(conversation_id, display_name)`` for the caller to announce to the UI
    *after* its commit succeeds (see _handle_introduction)."""
    try:
        await _verify_membership_advisory(sess, peer, gcm)
    except Exception:
        # Advisory: a bug here must never stop the message itself from being
        # handled. Without this, an exception leaves the MixWAL row
        # uncommitted, so the same message is re-read and re-raises
        # identically on every retry -- an infinite loop that permanently
        # stalls this peer's stream.
        logger.exception(
            "membership_hash advisory check raised; continuing without it"
        )
    handler = _HANDLERS.get(gcm.msg_type, _handle_chat)
    return await handler(sess, peer, gcm, full_payload)


async def _conversation_peers(
    sess: AsyncSession, conv_id: int
) -> "list[persistent.ConversationPeer]":
    rows = (await sess.exec(
        select(persistent.ConversationPeer)
        .where(
            persistent.ConversationPeer.id
            == persistent.ConversationPeerLink.conversation_peer_id
        )
        .where(persistent.ConversationPeerLink.conversation_id == conv_id)
    )).all()
    return list(rows)


async def local_membership_hash(
    sess: AsyncSession, conv: persistent.Conversation
) -> bytes:
    """Our own view of the conversation membership as the canonical hash
    (GROUP_CHAT_PROTOCOL.md 6b): every active, non-substream peer's read cap,
    plus ourself as ``write_cap[32:]`` rather than the possibly stale own-peer
    read cap."""
    peers = await _conversation_peers(sess, conv.id)
    caps: "set[bytes]" = set()
    for p in peers:
        if p.id == conv.own_peer_id:
            continue
        if not p.active or p.name.startswith(models.SUBSTREAM_NAME_PREFIX):
            continue
        rcw = await sess.get(persistent.ReadCapWAL, p.read_cap_id)
        if rcw is not None and rcw.read_cap is not None:
            caps.add(rcw.read_cap)
    wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
    if wcw is not None and wcw.write_cap is not None:
        caps.add(wcw.write_cap[32:])
    return models.canonical_membership_hash(caps)


async def membership_hash_for(conversation_id: int) -> bytes:
    """Convenience for the send choke points: open a session (on the io loop,
    reached via ``iothread.run_in_io`` -- never the Qt loop), load the
    conversation, and return its current local membership hash."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        return await local_membership_hash(sess, conv)


async def _verify_membership_advisory(
    sess: AsyncSession,
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
) -> None:
    """Advisory membership check: a sender that computed a real hash and
    disagrees with our view is logged, never dropped. Every shipping client
    still sends a sentinel, so this does no work until a real hash appears."""
    got = gcm.membership_hash
    if models.is_membership_sentinel(got):
        return
    local = await local_membership_hash(sess, peer.conversation)
    if got != local:
        logger.info(
            "membership_hash mismatch on conversation %s (advisory): peer "
            "sent %s, local view %s", peer.conversation.id, got.hex()[:16],
            local.hex()[:16],
        )


async def _handle_chat(sess, peer, gcm, full_payload) -> "tuple[bool, bool, None]":
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return True, False, None


async def _handle_introduction(sess, peer, gcm, full_payload) -> "tuple[bool, bool, tuple[int, str] | None]":
    """A member announced a newcomer: add the newcomer as a peer so their
    stream gets read, unless the announcement is about ourselves or someone we
    already know. The message itself is always stored, so every member's
    history shows where in the inductor's stream the newcomer joined.

    ``gcm.introduction.read_cap`` is the newcomer's salt-mutated read cap; the
    announcement about ourselves is recognised by comparing it against our own
    write cap, and an announcement for someone already present (possibly under
    an original, unmutated read cap we already hold) is skipped rather than
    polling the same stream twice.

    The peer-added notification (UI queue + read-loop wakeup) is NOT fired
    here: this runs inside the caller's transaction, which can still be
    rolled back (e.g. sqlite lock contention retried by
    drain_mixwal_read_single). Firing here would leak a notification for a
    peer that a retry then never actually commits. Instead the newcomer is
    returned for the caller to announce only once its commit has actually
    succeeded.
    """
    peer_added = None
    if intro := gcm.as_introduction:
        conv = peer.conversation
        own_cap = await persistent.own_read_cap(sess, conv)
        if own_cap != intro.read_cap and not await _already_has(sess, conv.id, intro):
            from .voucher import (
                MAX_GROUP_MEMBERS, _active_member_count, _add_peer, _sanitize_peer_name,
            )
            if await _active_member_count(sess, conv.id) >= MAX_GROUP_MEMBERS:
                logger.warning("conversation %s reached its member limit", conv.id)
            else:
                _add_peer(sess, conv, intro.display_name, intro.read_cap)
                peer_added = (conv.id, _sanitize_peer_name(intro.display_name))
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return True, False, peer_added


async def _already_has(sess, conv_id: int, intro: "GroupChatPleaseAdd") -> bool:
    """True if the conversation already has a peer with this exact read cap.

    The read cap is the newcomer's unique cryptographic identity; matching
    on it alone (rather than also treating a display_name match as "already
    known") avoids silently and permanently hiding a genuinely distinct
    member who happens to share a display name with someone already
    present, including ourselves — there is no uniqueness enforced on
    display names anywhere in the mint/induct flow.

    Delegates to ``persistent.peer_has_read_cap``, the same dedup check the
    voucher induction paths use for their "already inducted" guard, so one
    query stays correct everywhere. That helper uses an explicit join rather
    than relationship traversal: the receive path runs in SQLAlchemy's async
    session, where touching a ``conv.peers`` lazy relationship raises
    ``MissingGreenlet``.
    """
    if intro.read_cap is None:
        return False
    return await persistent.peer_has_read_cap(sess, conv_id, intro.read_cap)


async def _handle_tally(sess, peer, gcm, full_payload) -> "tuple[bool, bool, None]":
    signal_send = await tally_controller.handle_event(sess, peer, gcm)
    return False, signal_send, None


_CHAT_TYPES = (
    GroupChatTypeEnum.TEXT,
    GroupChatTypeEnum.FILE_UPLOAD,
    GroupChatTypeEnum.WHO,
    GroupChatTypeEnum.REPLY_WHO,
)
_TALLY_TYPES = (
    GroupChatTypeEnum.TALLY_CREATE,
    GroupChatTypeEnum.TALLY_VOTE,
    GroupChatTypeEnum.TALLY_CLOSE,
    GroupChatTypeEnum.TALLY_SYNC_REQ,
    GroupChatTypeEnum.TALLY_SYNC_RESP,
)

_HANDLERS = {
    GroupChatTypeEnum.INTRODUCTION: _handle_introduction,
    **{t: _handle_chat for t in _CHAT_TYPES},
    **{t: _handle_tally for t in _TALLY_TYPES},
}
