"""Drive the tally protocol over the group chat, free of Qt.

The controller owns one in-memory pycrdt ``Doc`` per survey and reconciles it
with the persisted blob in ``TallyState``. It has two faces:

* receive: :meth:`TallyController.handle_event` applies an inbound tally
  message to the local Doc. A vote is recorded under the *authenticated*
  sender's key (a hash of the peer's read capability), never an id taken from
  the payload, which is what makes "you cannot cast another member's vote"
  hold without a separate check.
* send: :meth:`create_local` and :meth:`cast_local_vote` mutate the local Doc
  for the user's own actions; the caller then transmits the corresponding
  message (see :mod:`katzenqt.tally.events` and :mod:`katzenqt.tally.send`).

Database work uses the session handed in by the caller and is not committed
here; the caller owns the transaction boundary.
"""
from __future__ import annotations

import hashlib
import logging

from pycrdt import Doc

from .. import persistent
from ..models import GroupChatMessage, GroupChatTypeEnum
from . import engine, schema, send, sync
from .events import build_sync_response

logger = logging.getLogger(__name__)


def voter_id_from_read_cap(read_cap: bytes) -> bytes:
    """The stable, peer-independent voter identity: a hash of the read
    capability bytes. Every member derives the same id for the same member,
    because they all hold the same read cap for them."""
    return hashlib.blake2b(read_cap, digest_size=16).digest()


async def _voter_id(sess, peer: "persistent.ConversationPeer") -> bytes:
    rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
    if rcw is not None and rcw.read_cap is not None:
        return voter_id_from_read_cap(rcw.read_cap)
    # Before the cap is provisioned there is nothing stable to key on; fall
    # back to the local id so a vote is not silently dropped in tests.
    logger.warning("no read cap provisioned for peer %r; using local id", peer.name)
    return voter_id_from_read_cap(peer.read_cap_id.bytes)


class TallyController:
    def __init__(self) -> None:
        self._docs: "dict[bytes, Doc]" = {}

    def get(self, survey_id: bytes) -> "Doc | None":
        return self._docs.get(survey_id)

    def surveys(self) -> "list[bytes]":
        return list(self._docs)

    async def load_all(self) -> None:
        """Populate the in-memory Docs from persisted state. Optional: the
        receive path also loads lazily on first reference."""
        async with persistent.asession() as sess:
            rows = (await sess.exec(persistent.select(persistent.TallyState))).all()
        for row in rows:
            self._docs[row.survey_id] = sync.load_doc(row.doc_state)

    async def _ensure_loaded(self, sess, survey_id: bytes) -> "Doc | None":
        doc = self._docs.get(survey_id)
        if doc is not None:
            return doc
        row = await sess.get(persistent.TallyState, survey_id)
        if row is None:
            return None
        doc = sync.load_doc(row.doc_state)
        self._docs[survey_id] = doc
        return doc

    async def _save(self, sess, survey_id: bytes, conversation_id: int) -> None:
        blob = sync.full_state(self._docs[survey_id])
        row = await sess.get(persistent.TallyState, survey_id)
        if row is None:
            sess.add(persistent.TallyState(
                survey_id=survey_id, conversation_id=conversation_id, doc_state=blob,
            ))
        else:
            row.doc_state = blob
            sess.add(row)

    async def create_local(self, sess, conversation, survey_id, topic, mode, slots) -> Doc:
        creator = await _voter_id(sess, conversation.own_peer)
        doc = schema.new_survey_doc(survey_id, topic, mode, slots, creator=creator)
        self._docs[survey_id] = doc
        await self._save(sess, survey_id, conversation.id)
        return doc

    async def close_local(self, sess, conversation, survey_id) -> bool:
        """Close the survey if the local user opened it. Returns False if the
        survey is unknown or the user is not its creator."""
        doc = await self._ensure_loaded(sess, survey_id)
        if doc is None:
            logger.warning("cannot close unknown survey %s", survey_id.hex())
            return False
        creator = schema.creator_of(doc)
        me = await _voter_id(sess, conversation.own_peer)
        if creator is not None and creator != me:
            logger.warning("refusing to close survey %s: not the creator", survey_id.hex())
            return False
        engine.close_survey(doc)
        await self._save(sess, survey_id, conversation.id)
        return True

    async def list_for_conversation(self, sess, conversation_id) -> "list[Doc]":
        """The survey Docs stored for a conversation, loaded from persisted state."""
        rows = (await sess.exec(persistent.select(persistent.TallyState).where(
            persistent.TallyState.conversation_id == conversation_id))).all()
        docs = []
        for row in rows:
            doc = self._docs.get(row.survey_id) or sync.load_doc(row.doc_state)
            self._docs[row.survey_id] = doc
            docs.append(doc)
        return docs

    async def cast_local_vote(self, sess, conversation, survey_id, choice) -> "int | None":
        """Record the user's own vote, minting the next version so a recast
        supersedes their prior one. Returns the version used, or ``None`` if the
        survey is unknown; the caller puts that version on the outbound event."""
        doc = await self._ensure_loaded(sess, survey_id)
        if doc is None:
            logger.warning("cannot vote on unknown survey %s", survey_id.hex())
            return None
        voter = await _voter_id(sess, conversation.own_peer)
        version = engine.current_version(doc, voter) + 1
        engine.apply_vote(doc, voter, choice, version)
        await self._save(sess, survey_id, conversation.id)
        return version

    async def handle_event(self, sess, peer, gcm: GroupChatMessage) -> bool:
        """Apply an inbound tally message to the local Doc. Returns True when
        outbound work was staged in ``sess`` and the send loop must be poked."""
        tally = gcm.tally
        if tally is None:
            logger.warning("tally message with no payload; dropping")
            return False
        survey_id = tally.survey_id
        conversation_id = peer.conversation.id
        kind = gcm.msg_type

        if kind is GroupChatTypeEnum.TALLY_CREATE:
            doc = self._docs.get(survey_id)
            if doc is None:
                self._docs[survey_id] = sync.load_doc(tally.crdt)
            else:
                sync.apply_update(doc, tally.crdt)
            await self._save(sess, survey_id, conversation_id)
            return False

        if kind is GroupChatTypeEnum.TALLY_VOTE:
            doc = await self._ensure_loaded(sess, survey_id)
            if doc is None:
                logger.warning("vote for unknown survey %s; dropping", survey_id.hex())
                return False
            voter = await _voter_id(sess, peer)
            try:
                engine.apply_vote(doc, voter, tally.choice or {}, tally.version)
            except ValueError as exc:
                logger.warning("rejecting invalid vote on %s: %s", survey_id.hex(), exc)
                return False
            await self._save(sess, survey_id, conversation_id)
            return False

        if kind is GroupChatTypeEnum.TALLY_CLOSE:
            doc = await self._ensure_loaded(sess, survey_id)
            if doc is None:
                return False
            creator = schema.creator_of(doc)
            sender = await _voter_id(sess, peer)
            if creator is not None and creator != sender:
                logger.warning("ignoring close of %s from a non-creator", survey_id.hex())
                return False
            engine.close_survey(doc)
            await self._save(sess, survey_id, conversation_id)
            return False

        if kind is GroupChatTypeEnum.TALLY_SYNC_RESP:
            doc = self._docs.get(survey_id)
            if doc is None:
                self._docs[survey_id] = sync.load_doc(tally.crdt)
            else:
                sync.apply_update(doc, tally.crdt)
            await self._save(sess, survey_id, conversation_id)
            return False

        if kind is GroupChatTypeEnum.TALLY_SYNC_REQ:
            doc = await self._ensure_loaded(sess, survey_id)
            if doc is None:
                return False
            diff = sync.diff_since(doc, tally.crdt or b"")
            await send.stage_outbound(sess, peer.conversation, build_sync_response(survey_id, diff))
            return True

        logger.warning("unhandled tally kind %s", kind)
        return False


# The process holds a single controller; the receive dispatch and the headless
# verbs share it so a survey created in one place is visible in the other.
INSTANCE = TallyController()


async def handle_event(sess, peer, gcm: GroupChatMessage) -> bool:
    return await INSTANCE.handle_event(sess, peer, gcm)
