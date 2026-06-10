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

from . import persistent
from .models import GroupChatTypeEnum
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


async def _handle_tally(sess, peer, gcm, full_payload) -> "tuple[bool, bool]":
    signal_send = await tally_controller.handle_event(sess, peer, gcm)
    return False, signal_send


_CHAT_TYPES = (
    GroupChatTypeEnum.TEXT,
    GroupChatTypeEnum.INTRODUCTION,
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
    **{t: _handle_chat for t in _CHAT_TYPES},
    **{t: _handle_tally for t in _TALLY_TYPES},
}
