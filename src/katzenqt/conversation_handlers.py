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
from typing import NamedTuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import models, persistent
from .models import GroupChatPleaseAdd, GroupChatTypeEnum
from .tally import controller as tally_controller

logger = logging.getLogger(__name__)


class PeerAnnouncement(NamedTuple):

    conversation_id: int
    display_name: str


class DispatchResult(NamedTuple):

    convlog_added: bool
    signal_send: bool
    peer_added: "PeerAnnouncement | None"
    tally_added: bool


async def dispatch(
    sess: AsyncSession,
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    full_payload: bytes,
) -> DispatchResult:
    """Handle ``gcm`` for ``peer``. See :class:`DispatchResult` for what each
    field means."""
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


async def _handle_chat(
    sess: AsyncSession,
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    full_payload: bytes,
) -> DispatchResult:
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return DispatchResult(True, False, None, False)


async def _handle_introduction(
    sess: AsyncSession,
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    full_payload: bytes,
) -> DispatchResult:
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
                peer_added = PeerAnnouncement(conv.id, _sanitize_peer_name(intro.display_name))
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return DispatchResult(True, False, peer_added, False)


async def _already_has(
    sess: AsyncSession, conv_id: int, intro: "GroupChatPleaseAdd",
) -> bool:
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


async def _handle_tally(
    sess: AsyncSession,
    peer: persistent.ConversationPeer,
    gcm: models.GroupChatMessage,
    full_payload: bytes,
) -> DispatchResult:
    result = await tally_controller.handle_event(sess, peer, gcm)
    # Every tally message is a chat row (displayed from its decoded payload),
    # so it is appended like any other; ``tally_added`` additionally tells the
    # caller to refresh the poll views.
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return DispatchResult(True, result.signal_send, None, True)


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
__all__ = [
    "AsyncSession",
    "GroupChatPleaseAdd",
    "GroupChatTypeEnum",
    "annotations",
    "dispatch",
    "logger",
    "logging",
    "models",
    "persistent",
    "select",
    "tally_controller",
]

