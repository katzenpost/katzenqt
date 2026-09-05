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


async def dispatch(sess, peer, gcm, full_payload) -> "tuple[bool, bool]":
    """Handle ``gcm`` for ``peer``. Returns ``(convlog_added, signal_send)``:
    whether a ConversationLog row was added (so the chat view is notified) and
    whether outbound work was staged that the send loop must be poked for."""
    handler = _HANDLERS.get(gcm.msg_type, _handle_chat)
    return await handler(sess, peer, gcm, full_payload)


async def _handle_chat(sess, peer, gcm, full_payload) -> "tuple[bool, bool]":
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return True, False


async def _handle_introduction(sess, peer, gcm, full_payload) -> "tuple[bool, bool]":
    """A member announced a newcomer: add the newcomer as a peer so their
    stream gets read, unless the announcement is about ourselves or someone we
    already know. The message itself is always stored, so every member's
    history shows where in the inductor's stream the newcomer joined.

    ``gcm.introduction.read_cap`` is the newcomer's salt-mutated read cap; the
    announcement about ourselves is recognised by comparing it against our own
    write cap, and an announcement for someone already present (possibly under
    an original, unmutated read cap we already hold) is skipped rather than
    polling the same stream twice.
    """
    intro = gcm.introduction
    if intro is not None:
        conv = peer.conversation
        wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
        own_cap = wcw.write_cap[32:] if wcw is not None and wcw.write_cap is not None else None
        if own_cap != intro.read_cap and not await _already_has(sess, conv.id, intro):
            from .voucher import _add_peer
            _add_peer(sess, conv, intro.display_name, intro.read_cap)
            from .network import readables_to_mixwal_event
            readables_to_mixwal_event.set()
    sess.add(persistent.ConversationLog.append_from(peer, full_payload))
    return True, False


async def _already_has(sess, conv_id: int, intro: "GroupChatPleaseAdd") -> bool:
    """True if the conversation already has a peer that this announcement
    addresses: the same name (a duplicate under a different read cap) or the
    same read cap (a re-announcement under a different name).

    Uses explicit queries rather than relationship traversal: the receive path
    runs in SQLAlchemy's async session, where touching a ``conv.peers`` lazy
    relationship raises ``MissingGreenlet``.
    """
    rows = (await sess.exec(
        select(persistent.ConversationPeer, persistent.ReadCapWAL)
        .join(
            persistent.ConversationPeerLink,
            persistent.ConversationPeerLink.conversation_peer_id
            == persistent.ConversationPeer.id,
        )
        .join(
            persistent.ReadCapWAL,
            persistent.ReadCapWAL.id == persistent.ConversationPeer.read_cap_id,
        )
        .where(persistent.ConversationPeerLink.conversation_id == conv_id)
    )).all()
    return any(
        peer.name == intro.display_name
        or (rcw is not None and rcw.read_cap == intro.read_cap)
        for peer, rcw in rows
    )


async def _handle_tally(sess, peer, gcm, full_payload) -> "tuple[bool, bool]":
    signal_send = await tally_controller.handle_event(sess, peer, gcm)
    return False, signal_send


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
