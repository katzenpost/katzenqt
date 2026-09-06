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

from . import persistent
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
    handler = _HANDLERS.get(gcm.msg_type, _handle_chat)
    return await handler(sess, peer, gcm, full_payload)


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
    intro = gcm.introduction
    if intro is not None:
        conv = peer.conversation
        wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
        own_cap = wcw.write_cap[32:] if wcw is not None and wcw.write_cap is not None else None
        if own_cap is None:
            # Own write cap not provisioned yet (a background loop fills it
            # in shortly after conversation creation); fall back to the
            # unmutated read cap, same as _build_who_reply does for the
            # symmetric case, so a self-announcement heard early isn't
            # misclassified as a stranger and added as our own peer.
            own_rcw = await sess.get(persistent.ReadCapWAL, conv.own_peer.read_cap_id)
            own_cap = own_rcw.read_cap if own_rcw is not None else None
        if own_cap != intro.read_cap and not await _already_has(sess, conv.id, intro):
            from .voucher import _add_peer
            _add_peer(sess, conv, intro.display_name, intro.read_cap)
            peer_added = (conv.id, intro.display_name)
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

    Uses an explicit query rather than relationship traversal: the receive path
    runs in SQLAlchemy's async session, where touching a ``conv.peers`` lazy
    relationship raises ``MissingGreenlet``.
    """
    read_caps = (await sess.exec(
        select(persistent.ReadCapWAL.read_cap)
        .join(
            persistent.ConversationPeer,
            persistent.ConversationPeer.read_cap_id == persistent.ReadCapWAL.id,
        )
        .join(
            persistent.ConversationPeerLink,
            persistent.ConversationPeerLink.conversation_peer_id
            == persistent.ConversationPeer.id,
        )
        .where(persistent.ConversationPeerLink.conversation_id == conv_id)
    )).all()
    return intro.read_cap in read_caps


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
