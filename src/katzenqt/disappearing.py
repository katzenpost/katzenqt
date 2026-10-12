"""Disappearing messages: delete the content the user's retention policy no
longer keeps from a conversation's log. Introductions are kept, since a
message that disappears never takes a roster change with it, and our own
messages are kept until sent. See "Disappearing messages" in the group chat
spec; the Sent box's copy of our messages is dropped by the group state
machine, through ``acks``."""

from __future__ import annotations

import cbor2
from sqlmodel import col, select

from . import attachments, persistent

DAY_SECONDS = 24 * 3600


def _is_content(payload: bytes) -> bool:
    """
    >>> _is_content(b"F" + cbor2.dumps({"text": "hi"}))
    True
    >>> _is_content(b"F" + cbor2.dumps({"introduction": {"display_name": "bob"}}))
    False
    >>> _is_content(b"Your name in this conversation is me")
    False
    >>> _is_content(b"F" + bytes([255]))
    False
    """
    if payload[:1] != b"F":
        return False
    try:
        decoded = cbor2.loads(payload[1:])
    except Exception:
        return False
    return isinstance(decoded, dict) and decoded.get("introduction") is None


async def expire(now: float) -> list[int]:
    """Delete what each conversation's policy no longer keeps, as of
    ``now``; returns the conversations changed."""
    async with persistent.asession() as sess:
        conversations = list(await sess.exec(select(persistent.Conversation)))
    changed = []
    for conv in conversations:
        async with persistent.conversation_log_order_lock(conv.id):
            async with persistent.asession() as sess:
                policy = await persistent.retention(sess, conv.id)
                if policy == persistent.Retention():
                    continue
                rows = await sess.exec(
                    select(persistent.ConversationLog).where(
                        col(persistent.ConversationLog.conversation_id) == conv.id
                    )
                )
                doomed = []
                for row in rows:
                    own = row.conversation_peer_id == conv.own_peer_id
                    days = policy.sent_days if own else policy.received_days
                    if (
                        days is None
                        or row.logged_at is None
                        or now - row.logged_at < days * DAY_SECONDS
                        or (own and row.network_status != 2)
                        or not _is_content(row.payload)
                    ):
                        continue
                    doomed.append(row)
                if not doomed:
                    continue
                files = set().union(*(attachments.paths_in(row.payload) for row in doomed))
                for row in doomed:
                    await sess.delete(row)
                await sess.flush()
                files -= await attachments.remaining(sess, conv.id)
                await sess.commit()
        attachments.unlink(files)
        changed.append(conv.id)
    return changed
