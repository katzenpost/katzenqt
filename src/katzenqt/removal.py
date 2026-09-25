"""Remove a group chat, or one member of it, from the local state file.

Removal is local: nothing is sent to the other members. It stops reading the
removed streams and deletes every row that names them, so the state file keeps
no trace of the chat or the member. This module is free of Qt.
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import NamedTuple

import cbor2
import sqlalchemy as sa
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import conversation_handlers, models, network, persistent
from .tally import controller as tally_controller

logger = logging.getLogger(__name__)


class RemovalError(ValueError):
    """The requested removal does not apply to this conversation or peer."""


class _Doomed(NamedTuple):
    peer_ids: "list[int]"
    read_streams: "list[uuid.UUID]"
    write_streams: "list[uuid.UUID]"

    @property
    def streams(self) -> "list[uuid.UUID]":
        return self.read_streams + self.write_streams


def _attachment_paths(payload: bytes) -> "set[str]":
    if payload[:1] != b"F":
        return set()
    try:
        decoded = cbor2.loads(payload[1:])
    except Exception:
        return set()
    if not isinstance(decoded, dict):
        return set()
    keys = ("rel_path", "thumb_rel_path")
    return {p for p in (decoded.get(k) for k in keys) if isinstance(p, str)}


def _attachment_file(rel_path: str) -> "Path | None":
    root = (persistent.state_file.parent / "attachments").resolve()
    path = (persistent.state_file.parent / rel_path).resolve()
    return path if path.is_relative_to(root) else None


def _unlink_attachments(rel_paths: "set[str]") -> None:
    for rel_path in rel_paths:
        path = _attachment_file(rel_path)
        if path is None:
            logger.warning("not deleting attachment outside the state dir: %r", rel_path)
            continue
        path.unlink(missing_ok=True)


async def _remaining_attachment_paths(
    sess: AsyncSession, conversation_id: int,
) -> "set[str]":
    payloads = (await sess.exec(
        select(persistent.ConversationLog.payload).where(
            persistent.ConversationLog.conversation_id == conversation_id,
        )
    )).all()
    return set().union(*(_attachment_paths(p) for p in payloads))


async def _silence_peers(conversation_id: int, peer_id: int) -> _Doomed:
    """Mark the peer and its download substreams inactive and paused, and
    commit, so the arming sweep stops re-arming them while they are torn down."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        if conv is None:
            raise RemovalError(f"no conversation {conversation_id}")
        if peer_id == conv.own_peer_id:
            raise RemovalError("cannot remove yourself; remove the conversation")
        peers = await conversation_handlers._conversation_peers(sess, conversation_id)
        target = next((p for p in peers if p.id == peer_id), None)
        if target is None:
            raise RemovalError(f"peer {peer_id} is not in conversation {conversation_id}")
        prefix = f"{models.SUBSTREAM_NAME_PREFIX}{peer_id}:"
        doomed = [target] + [p for p in peers if p.name.startswith(prefix)]
        for peer in doomed:
            peer.active = False
            sess.add(peer)
            rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is not None:
                rcw.paused = True
                sess.add(rcw)
        result = _Doomed([p.id for p in doomed], [p.read_cap_id for p in doomed], [])
        await sess.commit()
        return result


async def _delete_read_state(sess: AsyncSession, streams: "list[uuid.UUID]") -> None:
    await sess.exec(sa.delete(persistent.MixWAL).where(
        persistent.MixWAL.bacap_stream.in_(streams),
    ))
    await sess.exec(sa.delete(persistent.ReceivedPiece).where(
        persistent.ReceivedPiece.read_cap.in_(streams),
    ))


async def _delete_unshared_read_caps(
    sess: AsyncSession, streams: "list[uuid.UUID]",
) -> None:
    shared = set((await sess.exec(
        select(persistent.ConversationPeer.read_cap_id).where(
            persistent.ConversationPeer.read_cap_id.in_(streams),
        )
    )).all())
    gone = [s for s in streams if s not in shared]
    await sess.exec(sa.delete(persistent.ReadCapWAL).where(
        persistent.ReadCapWAL.id.in_(gone),
    ))


async def _delete_peer_rows(
    sess: AsyncSession, conversation_id: int, doomed: _Doomed,
) -> "set[str]":
    logs = (await sess.exec(select(persistent.ConversationLog).where(
        persistent.ConversationLog.conversation_id == conversation_id,
        persistent.ConversationLog.conversation_peer_id.in_(doomed.peer_ids),
    ))).all()
    candidates = set().union(*(_attachment_paths(row.payload) for row in logs))
    for row in logs:
        await sess.delete(row)
    await sess.flush()
    await _delete_read_state(sess, doomed.read_streams)
    await sess.exec(sa.delete(persistent.ConversationPeerLink).where(
        persistent.ConversationPeerLink.conversation_peer_id.in_(doomed.peer_ids),
    ))
    await sess.exec(sa.delete(persistent.ConversationPeer).where(
        persistent.ConversationPeer.id.in_(doomed.peer_ids),
    ))
    await _delete_unshared_read_caps(sess, doomed.read_streams)
    return candidates - await _remaining_attachment_paths(sess, conversation_id)


def _announce_removed(streams: "list[uuid.UUID]") -> None:
    for stream in streams:
        network.substream_progress_queue.put_nowait(("removed", stream))


async def remove_peer(*, conversation_id: int, peer_id: int) -> None:
    """Stop reading ``peer_id`` and forget them: their read stream, download
    substreams, received pieces and the messages they wrote."""
    doomed = await _silence_peers(conversation_id, peer_id)
    for stream in doomed.streams:
        await network.stop_stream(stream)
    async with persistent.asession() as sess:
        orphaned = await _delete_peer_rows(sess, conversation_id, doomed)
        await sess.commit()
    _unlink_attachments(orphaned)
    _announce_removed(doomed.read_streams[1:])
    network.readables_to_mixwal_event.set()


async def _silence_conversation(conversation_id: int) -> _Doomed:
    """Mark every peer inactive and every stream paused, and commit, so the
    read arming and write sweeps stop touching them while they are torn down."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        if conv is None:
            raise RemovalError(f"no conversation {conversation_id}")
        peers = await conversation_handlers._conversation_peers(sess, conversation_id)
        pwal_streams = (await sess.exec(
            select(persistent.PlaintextWAL.bacap_stream).where(
                persistent.PlaintextWAL.conversation_id == conversation_id,
            ).distinct()
        )).all()
        write_streams = list({conv.write_cap, *pwal_streams})
        for peer in peers:
            peer.active = False
            sess.add(peer)
            rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is not None:
                rcw.paused = True
                sess.add(rcw)
        for stream in write_streams:
            wcw = await sess.get(persistent.WriteCapWAL, stream)
            if wcw is not None:
                wcw.paused = True
                sess.add(wcw)
        result = _Doomed(
            [p.id for p in peers], [p.read_cap_id for p in peers], write_streams,
        )
        await sess.commit()
        return result


async def _delete_outbound_state(
    sess: AsyncSession, conversation_id: int, doomed: _Doomed,
) -> "list[uuid.UUID]":
    """Delete the conversation's send queue, sent-log entries and log; returns
    the upload indirection read caps that now have no owner."""
    pwals = (await sess.exec(select(persistent.PlaintextWAL).where(
        persistent.PlaintextWAL.conversation_id == conversation_id,
    ))).all()
    pwal_ids = [p.id for p in pwals]
    indirections = [p.indirection for p in pwals if p.indirection is not None]
    sent_ids = (await sess.exec(select(persistent.ConversationLog.outgoing_pwal).where(
        persistent.ConversationLog.conversation_id == conversation_id,
        persistent.ConversationLog.outgoing_pwal.is_not(None),
    ))).all()
    await sess.exec(sa.delete(persistent.ConversationLog).where(
        persistent.ConversationLog.conversation_id == conversation_id,
    ))
    await sess.exec(sa.delete(persistent.SentLog).where(
        persistent.SentLog.id.in_([*sent_ids, *pwal_ids]),
    ))
    await sess.exec(sa.delete(persistent.MixWAL).where(sa.or_(
        persistent.MixWAL.plaintextwal.in_(pwal_ids),
        persistent.MixWAL.bacap_stream.in_(doomed.write_streams),
    )))
    await sess.exec(sa.delete(persistent.PlaintextWAL).where(
        persistent.PlaintextWAL.conversation_id == conversation_id,
    ))
    return indirections


async def _delete_conversation_rows(
    sess: AsyncSession, conversation_id: int, doomed: _Doomed,
) -> "list[uuid.UUID]":
    indirections = await _delete_outbound_state(sess, conversation_id, doomed)
    await _delete_read_state(sess, doomed.read_streams)
    for model in (persistent.TallyState, persistent.PendingVoucher):
        await sess.exec(sa.delete(model).where(model.conversation_id == conversation_id))
    await sess.exec(sa.delete(persistent.ConversationPeerLink).where(
        persistent.ConversationPeerLink.conversation_id == conversation_id,
    ))
    await sess.exec(sa.delete(persistent.Conversation).where(
        persistent.Conversation.id == conversation_id,
    ))
    await sess.exec(sa.delete(persistent.ConversationPeer).where(
        persistent.ConversationPeer.id.in_(doomed.peer_ids),
    ))
    await _delete_unshared_read_caps(sess, doomed.read_streams + indirections)
    await sess.exec(sa.delete(persistent.WriteCapWAL).where(
        persistent.WriteCapWAL.id.in_(doomed.write_streams),
    ))
    return indirections


async def remove_conversation(*, conversation_id: int) -> None:
    """Stop every stream of the conversation and delete it: members, read and
    write caps, queued and received data, polls, vouchers and attachments."""
    doomed = await _silence_conversation(conversation_id)
    for stream in doomed.streams:
        await network.stop_stream(stream)
    async with persistent.asession() as sess:
        indirections = await _delete_conversation_rows(sess, conversation_id, doomed)
        await sess.commit()
    shutil.rmtree(
        persistent.state_file.parent / "attachments" / str(conversation_id),
        ignore_errors=True,
    )
    tally_controller.INSTANCE.forget_conversation(conversation_id)
    _announce_removed(doomed.read_streams + indirections)
    network.readables_to_mixwal_event.set()
