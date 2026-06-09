"""Stage a tally message onto a conversation's outgoing BACAP stream.

This is the data half of sending: it serialises a ``GroupChatMessage`` into
PlaintextWAL rows on the conversation's write cap and adds them to the caller's
session. The caller commits and then pokes the send loop
(``network.check_for_new``). It is free of Qt and of any view concern.

Outgoing writes go on the conversation's ``write_cap`` (the WriteCapWAL primary
key); ``find_resendable`` requires the row's ``bacap_stream`` to match a fully
provisioned WriteCapWAL, so any other stream would silently stall.
"""
from __future__ import annotations

import uuid

from .. import models, persistent

_CHUNK_SIZE = 1530


async def stage_outbound(sess, conversation: "persistent.Conversation", gcm: "models.GroupChatMessage") -> uuid.UUID:
    """Serialise ``gcm`` and stage its rows in ``sess``. Returns the id of the
    final PlaintextWAL, which lands in SentLog once the message has cleared."""
    send_op = models.SendOperation(bacap_stream=conversation.write_cap, messages=[gcm])
    new_write_caps, db_entries = send_op.serialize(
        chunk_size=_CHUNK_SIZE, conversation_id=conversation.id,
    )
    for cap_uuid in new_write_caps:
        sess.add(persistent.WriteCapWAL(id=cap_uuid))
    for obj in db_entries:
        sess.add(obj)
    return db_entries[-1].id
