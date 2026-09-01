"""Contact Voucher handshake, driven over kpclientd.

Implements the Contact Voucher protocol:
  https://katzenpost.network/docs/specs/contact_voucher/
  https://katzenpost.network/docs/specs/contact_voucher_narration/

A joiner mints a single Voucher, hands it out of band to an existing member, and
the member inducts them; the reply travels back over the rendezvous VoucherStream,
so no second token is exchanged.

The handshake is a fixed two-box exchange (box 0: the joiner's VoucherPayload;
box 1: the inductor's sealed VoucherReply) on a stream derived entirely from the
Voucher. It is deliberately kept out of the chat MixWAL/PlaintextWAL loops: those
assume an open-ended framed stream with freshly-minted caps, whereas this is two
raw boxes on pre-derived caps. Durable state lives in persistent.PendingVoucher,
advanced before each network step so a crash mid-handshake resumes rather than
restarts. All cap and key material is opaque bytes; the daemon does the crypto.
"""
import asyncio
import logging
import uuid

from katzenpost_thinclient import (
    BoxIDNotFoundError, CourierError, CourierInvalidEpochError,
    DatabaseFailureError, InvalidEpochError, ThinClientOfflineError,
)
from sqlmodel import select

from . import models, persistent
from .katzen_util import create_task
from .network import _SUBSTREAM_NAME_PREFIX, check_for_new, conversation_update_queue

logger = logging.getLogger("katzen.voucher")

STEP_MINTED = "minted"
STEP_AWAITING = "awaiting"
STEP_INDUCTING = "inducting"
STEP_DONE = "done"

_INDEX_LEN = 104
_READ_RETRY_GAP_S = 15.0  # bounded round gap, well inside a ~60s PKI epoch window
_STALL_WARN_ROUNDS = 40  # ~10 minutes of continuous errors before escalating to WARNING


def _brief(b: "bytes | None") -> str:
    """Short first/last hex of a cap or box index for log lines."""
    if not b:
        return "None"
    return b[:8].hex() + ".." + b[-8:].hex()


class AlreadyJoinedError(Exception):
    """Minting a voucher for a conversation the client already belongs to would
    re-run the join, duplicating members and messages."""


class PendingVoucherExistsError(Exception):
    """A conversation already has an unfinished voucher in flight; cancel it
    before minting another."""


async def conversation_is_joined(conversation_id: int) -> bool:
    """True if the conversation already has a real member: an active peer that
    is not a synthetic substream peer. The client's own peer is inactive, so it
    does not count."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        if conv is None:
            return False
        return any(
            p.active and not p.name.startswith(_SUBSTREAM_NAME_PREFIX)
            for p in conv.peers
        )


async def pending_voucher_for(conversation_id: int, role: str = "joiner") -> "uuid.UUID | None":
    """The id of an unfinished voucher for this conversation and role, if any."""
    async with persistent.asession() as sess:
        row = (await sess.exec(
            select(persistent.PendingVoucher).where(
                persistent.PendingVoucher.conversation_id == conversation_id,
                persistent.PendingVoucher.role == role,
            )
        )).first()
        return row.id if row is not None else None


async def list_pending_vouchers() -> "list[tuple]":
    """Every unfinished voucher as (id, conversation_name, role, step), for the
    pending-voucher view."""
    out = []
    async with persistent.asession() as sess:
        for pv in (await sess.exec(select(persistent.PendingVoucher))).all():
            conv = await sess.get(persistent.Conversation, pv.conversation_id)
            out.append((pv.id, conv.name if conv is not None else "?", pv.role, pv.step))
    return out


async def cancel_pending_voucher(pending_id) -> None:
    """Delete a pending voucher row, abandoning that half-finished handshake."""
    async with persistent.asession() as sess:
        row = await sess.get(persistent.PendingVoucher, pending_id)
        if row is not None:
            await sess.delete(row)
            await sess.commit()


async def pending_joiner_join_conversation_ids() -> "list[int]":
    """Conversation ids whose joiner handshake a restart should resume.

    The joiner is net-promised a reply on the rendezvous stream only after the
    inductor acts, which may be after a crash/relaunch (or the reply may arrive
    while the app is down). Such vouchers are stuck in the DB precisely so a
    restart can pick them back up. ``awaiting`` is the only step with a persisted
    box-1 index we can poll yet; ``minted`` lacks it and is abandoned (the minted
    box 0 would duplicate if re-run)."""
    async with persistent.asession() as sess:
        rows = (await sess.exec(
            select(persistent.PendingVoucher).where(
                persistent.PendingVoucher.role == "joiner",
                persistent.PendingVoucher.step == STEP_AWAITING,
            )
        )).all()
        return [r.conversation_id for r in rows]


async def _publish_box(connection, write_cap: bytes, message_box_index: bytes, payload: bytes) -> bytes:
    """Write payload to one box and return the next box index."""
    wcr = await connection.encrypt_write(
        plaintext=payload, write_cap=write_cap, message_box_index=message_box_index,
    )
    await connection.start_resending_encrypted_message(
        read_cap=None, write_cap=write_cap, message_box_index=None, reply_index=None,
        envelope_descriptor=wcr.envelope_descriptor,
        message_ciphertext=wcr.message_ciphertext,
        envelope_hash=wcr.envelope_hash,
    )
    logger.debug(
        "publish_box: wrote box %s on write_cap %s; next box index %s",
        _brief(message_box_index), _brief(write_cap), _brief(wcr.next_message_box_index),
    )
    return wcr.next_message_box_index


async def _read_box(
    connection, read_cap: bytes, message_box_index: bytes, *, stage: str = "read_box",
) -> "tuple[bytes, bytes]":
    """Read one box, blocking until it exists, and return (plaintext, next index).

    TODO(workaround) — the daemon read ride-out goes stale across PKI epochs.
    ``start_resending_encrypted_message`` (no_retry_on_box_id_not_found=False)
    retransmits a single *epoch-bound* envelope — the ciphertext/descriptor pair
    built by one ``encrypt_read`` — with uncapped BoxIDNotFound retries, and never
    re-encrypts it. As the network rolls PKI epochs (~60s), the courier replica
    moves outside the envelope's tolerance window and rejects it (CourierInvalid-
    EpochError: "replica epoch outside tolerance window"), but the ride-out swallows
    the rejection into more retries on the same envelope, so the caller blocks
    forever with no error. pigeonhole.py:60 documents the need to watch the PKI doc
    and cancel+re-encrypt, so building a fresh envelope is conventionally the
    *caller's* job — yet the ride-out gives the caller no signal it has gone stale.

    The GUI join stall is this defect at minute scale: the joiner mints and polls
    its box-1 reply long before the inductor reads box 0 and writes that reply, so
    the wait spans several epochs and the box arrives at an index the stale read can
    never collect (instrumented logs: the joiner polled across epochs 2436105 →
    2436108, then a writer created that identical box — not an index mismatch).

    Until the upstream fix lands (re-encrypt per epoch inside the ride-out, or
    surface InvalidEpoch/CourierInvalidEpoch so clients can re-issue), each round
    here is a *fresh* request with ``no_retry_on_box_id_not_found=True``: a missing
    box errors out immediately, we sleep a bounded gap (well under an epoch,
    _READ_RETRY_GAP_S) and re-issue, so the wait always speaks the current epoch and
    replication state. When the upstream fix lands this loop collapses back to a
    single ride-out call.
    """
    started = asyncio.get_event_loop().time()
    rounds = 0
    while True:
        rounds += 1
        try:
            rcr = await connection.encrypt_read(
                read_cap=read_cap, message_box_index=message_box_index,
            )
            resp = await connection.start_resending_encrypted_message(
                read_cap=read_cap, write_cap=None,
                message_box_index=message_box_index, reply_index=None,
                envelope_descriptor=rcr.envelope_descriptor,
                message_ciphertext=rcr.message_ciphertext,
                envelope_hash=rcr.envelope_hash,
                no_retry_on_box_id_not_found=True,
            )
            logger.debug(
                "%s: box %s on read_cap %s returned after %.1fs "
                "(round %d, next=%s)",
                stage, _brief(message_box_index), _brief(read_cap),
                asyncio.get_event_loop().time() - started, rounds,
                _brief(rcr.next_message_box_index),
            )
            return resp.plaintext, rcr.next_message_box_index
        except (BoxIDNotFoundError, InvalidEpochError, CourierInvalidEpochError,
                DatabaseFailureError, CourierError, ThinClientOfflineError) as e:
            # Box not written/replicated yet, or the request reached a stale
            # epoch: both are expected mid-handshake and cured by a fresh
            # round. A storage replica or courier hiccup (DatabaseFailureError
            # / CourierError), or a momentary daemon disconnect
            # (ThinClientOfflineError), are the same "transient, retry" cases
            # drain_mixwal_read_single already treats as recoverable, so
            # treat them the same way here rather than aborting the whole
            # induction on one blip.
            if rounds == 1 or rounds % 4 == 0:
                logger.debug(
                    "%s: box %s on read_cap %s not present yet after %.1fs "
                    "(round %d, %s); retrying in %.0fs",
                    stage, _brief(message_box_index), _brief(read_cap),
                    asyncio.get_event_loop().time() - started, rounds,
                    type(e).__name__, _READ_RETRY_GAP_S,
                )
            if rounds == _STALL_WARN_ROUNDS or rounds % _STALL_WARN_ROUNDS == 0:
                # This wait is intentionally unbounded (it may legitimately
                # be waiting on a human to act), but a source of errors that
                # never clears deserves to be surfaced somewhere a user
                # could notice, not just another debug line every ~60s.
                logger.warning(
                    "%s: box %s on read_cap %s still not present after "
                    "%.0f minutes (round %d, latest: %s); still retrying",
                    stage, _brief(message_box_index), _brief(read_cap),
                    (asyncio.get_event_loop().time() - started) / 60.0,
                    rounds, type(e).__name__,
                )
            await asyncio.sleep(_READ_RETRY_GAP_S)


async def _conversation_write_cap(sess, conversation_id: int) -> persistent.WriteCapWAL:
    conv = await sess.get(persistent.Conversation, conversation_id)
    wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
    if wcw is None or wcw.write_cap is None:
        raise RuntimeError(
            f"conversation {conversation_id} has no provisioned write cap yet"
        )
    return wcw


def _sanitize_peer_name(name: str) -> str:
    """A peer-supplied display name, made safe to store as a ConversationPeer
    name. Strips C0/C1 control characters (which could break the substream
    name parse or spoof the display) and neutralises the reserved
    ``:substream:`` prefix so a peer cannot masquerade as a synthetic
    substream peer and have their messages routed onto another peer's log.

    Pure and total: never raises, always returns a non-empty string.
    """
    cleaned = "".join(
        ch for ch in (name or "") if not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F)
    )
    while cleaned.startswith(_SUBSTREAM_NAME_PREFIX):
        cleaned = cleaned[len(_SUBSTREAM_NAME_PREFIX):]
    return cleaned or "unnamed"


def _add_peer(sess, conversation, name: str, read_cap: "bytes | None") -> None:
    if not read_cap or len(read_cap) != _INDEX_LEN + 32:
        # 136 bytes total: a 32-byte public key plus the 104-byte index. A
        # None or malformed read_cap (e.g. a who-reply entry sent before its
        # sender's own cap was provisioned, see _build_who_reply) must not
        # crash the caller on the slice below; the peer just isn't added and
        # will need a later announcement to catch up.
        logger.warning(
            "_add_peer: refusing to add %r with malformed read_cap (%d bytes)",
            name, len(read_cap) if read_cap else 0,
        )
        return
    rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=read_cap, next_index=read_cap[-_INDEX_LEN:],
    )
    sess.add(rcw)
    sess.add(persistent.ConversationPeer(
        name=_sanitize_peer_name(name), read_cap_id=rcw.id, active=True,
        conversation=conversation,
    ))


async def mint_and_publish(connection, conversation_id: int, display_name: str) -> bytes:
    """Joiner: mint a Voucher over this conversation's MessageStream, publish the
    payload to VoucherStream box 0, and return the Voucher to share out of band.

    Refuses to mint for a conversation the client already belongs to, or one that
    already has a voucher in flight: joining twice duplicates members and
    messages. These checks run before any network IO, so a caller can pass a
    dead connection and still rely on them."""
    if await conversation_is_joined(conversation_id):
        raise AlreadyJoinedError(
            f"already a member of conversation {conversation_id}; no voucher needed"
        )
    if await pending_voucher_for(conversation_id) is not None:
        raise PendingVoucherExistsError(
            f"conversation {conversation_id} already has a pending voucher"
        )
    async with persistent.asession() as sess:
        wcw = await _conversation_write_cap(sess, conversation_id)
        message_write_cap = wcw.write_cap

    mint = await connection.voucher_mint(
        message_write_cap=message_write_cap, display_name=display_name,
    )

    pv = persistent.PendingVoucher(
        role="joiner", conversation_id=conversation_id, step=STEP_MINTED,
        voucher=mint.voucher,
        voucher_write_cap=mint.voucher_write_cap,
        voucher_read_cap=mint.voucher_read_cap,
        voucher_secret_key=mint.voucher_secret_key,
        display_name=display_name,
    )
    async with persistent.asession() as sess:
        sess.add(pv)
        await sess.commit()
        await sess.refresh(pv)
        pv_id = pv.id

    box1_index = await _publish_box(
        connection, mint.voucher_write_cap, mint.voucher_write_cap[-_INDEX_LEN:],
        mint.voucher_payload,
    )

    async with persistent.asession() as sess:
        row = await sess.get(persistent.PendingVoucher, pv_id)
        row.box1_index = box1_index
        row.step = STEP_AWAITING
        sess.add(row)
        await sess.commit()
    logger.debug(
        "mint_and_publish: published box0 on voucher write_cap %s; poll of box 1 at %s",
        _brief(mint.voucher_write_cap), _brief(box1_index),
    )
    return mint.voucher


async def await_and_open(connection, conversation_id: int) -> "list[str]":
    """Joiner: poll VoucherStream box 1 for the inductor's reply, open it, move
    this conversation's write cap onto the salt-mutated sequence, and add the
    members named in the reply as peers. Returns the display names added, so a
    caller (the GUI) can reflect them without re-querying.

    The poll only needs the persisted box-1 index, so a fresh connection (e.g.
    after a restart, or on the very first ``voucher-await``) resumes an
    unfinished handshake from its current point; see
    ``pending_joiner_join_conversation_ids``."""
    async with persistent.asession() as sess:
        pv = (await sess.exec(
            select(persistent.PendingVoucher).where(
                persistent.PendingVoucher.conversation_id == conversation_id,
                persistent.PendingVoucher.role == "joiner",
            )
        )).first()
        if pv is None:
            raise RuntimeError(f"no pending joiner voucher for conversation {conversation_id}")
        pv_id, voucher_read_cap, box1_index, secret_key = (
            pv.id, pv.voucher_read_cap, pv.box1_index, pv.voucher_secret_key,
        )

    sealed_reply, _ = await _read_box(
        connection, voucher_read_cap, box1_index, stage="await_and_open(box1)",
    )
    logger.debug(
        "await_and_open: received box1 sealed reply on voucher read_cap %s",
        _brief(voucher_read_cap),
    )

    async with persistent.asession() as sess:
        wcw = await _conversation_write_cap(sess, conversation_id)
        message_write_cap = wcw.write_cap

    opened = await connection.voucher_open(
        voucher_secret_key=secret_key, sealed_reply=sealed_reply,
        message_write_cap=message_write_cap,
    )
    reply_who = models.GroupChatReplyWho.from_cbor(opened.who_reply)

    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
        wcw.write_cap = opened.mutated_message_write_cap
        wcw.next_index = opened.mutated_message_write_cap[-_INDEX_LEN:]
        sess.add(wcw)
        added = []
        for please_add in reply_who.please_adds:
            if await persistent.peer_has_read_cap(
                sess, conversation_id, please_add.read_cap,
            ):
                # This member was already added by an earlier run of this
                # open (or an announcement that beat it here); adding a
                # second peer for the same read cap would read their
                # stream twice.
                logger.warning(
                    "await_and_open: %r already holds read cap %s on "
                    "conversation %d; skipping duplicate _add_peer",
                    please_add.display_name, _brief(please_add.read_cap),
                    conversation_id,
                )
            else:
                _add_peer(sess, conv, please_add.display_name, please_add.read_cap)
                added.append(please_add.display_name)
        row = await sess.get(persistent.PendingVoucher, pv_id)
        await sess.delete(row)
        await sess.commit()
    return added


async def _write_introduction_log(conversation_id: int, display_name: str, read_cap: bytes) -> "uuid.UUID":
    """Write the INTRODUCTION ConversationLog/PlaintextWAL rows. Returns the
    final PlaintextWAL id, for the caller to wait on the ack."""
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8,
        msg_type=models.GroupChatTypeEnum.INTRODUCTION,
        introduction=models.GroupChatPleaseAdd(
            display_name=display_name, read_cap=read_cap,
        ),
    )
    async with persistent.conversation_log_order_lock(conversation_id):
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conversation_id)
            send_op = models.SendOperation(bacap_stream=conv.write_cap, messages=[gcm])
            new_write_caps, db_entries = send_op.serialize(
                chunk_size=1530, conversation_id=conversation_id,
            )
            final_pwal_id = db_entries[-1].id
            for cap_uuid in new_write_caps:
                sess.add(persistent.WriteCapWAL(id=cap_uuid))
            for obj in db_entries:
                sess.add(obj)
            sess.add(persistent.ConversationLog(
                conversation_id=conversation_id,
                conversation_peer_id=conv.own_peer_id,
                conversation_order=persistent.next_conversation_order(conversation_id),
                payload=b"F" + gcm.to_cbor(),
                network_status=1,
                outgoing_pwal=final_pwal_id,
            ))
            await sess.commit()
    return final_pwal_id


async def send_introduction_message(conversation_id: int, display_name: str, read_cap: bytes) -> None:
    """Write an INTRODUCTION message onto this conversation's own BACAP stream.

    The announcement carries ``read_cap`` for the just-inducted member's
    stream (their salt-mutated read cap), so every peer that reads this
    stream can add the member and start reading their messages without any
    further coordination. It is sent after the induction has committed and is
    genuinely fire-and-forget: a failure (writing the announcement, or its
    eventual delivery) is logged, never raised, so the induction result
    (already durable by the time this is called) always stands.

    A pending ConversationLog row is also written for the sender's own peer,
    so the local UI shows the announcement (e.g. 'bob added carol') at the
    right place in the stream even though the sender never reads its own
    stream.
    """
    try:
        final_pwal_id = await _write_introduction_log(conversation_id, display_name, read_cap)
    except Exception as e:
        logger.error(
            "send_introduction_message: failed to write INTRODUCTION for "
            "%r in conversation %d: %s", display_name, conversation_id, e,
        )
        return

    # The UI's ConversationLogModel maps index_row 1:1 to conversation_order
    # and grows row_count by one per `False` event. Every other path that
    # appends a ConversationLog row emits this; if the sender's own
    # announcement doesn't, the view silently falls behind by one row per
    # announcement (the newest messages stay invisible until another message
    # nudges the window).
    await conversation_update_queue.put((conversation_id, False))

    await check_for_new()
    # Background, not awaited: this function's own contract is
    # fire-and-forget, so a caller (e.g. derive_read_and_induct, right after
    # durably committing the induction) must not be made to wait up to 180s
    # -- or see an exception from -- confirming delivery of the announcement.
    create_task(_wait_intro_acked(final_pwal_id, display_name, conversation_id))


async def _wait_intro_acked(final_pwal_id, display_name: str, conversation_id: int) -> None:
    """Wait until the INTRODUCTION announcement is acked into SentLog.

    Fire-and-forget: a timeout is logged, never raised, so the induction
    result stands even if the announcement never gets delivered.
    """
    if not await persistent.wait_for_sent(final_pwal_id, deadline_s=180.0):
        logger.error(
            "introduction for %r not acked within 180s (conversation %d)",
            display_name, conversation_id,
        )


async def derive_read_and_induct(
    connection, conversation_id: int, peer_name: str, voucher: bytes,
) -> "str | None":
    """Inductor: derive the VoucherStream from the Voucher, read the joiner's
    payload from box 0, seal a reply carrying the group's read caps, write it to
    box 1, and add the joiner (on their salt-mutated read cap) as a peer. Returns
    the joiner's display name, or None if this joiner had already been
    inducted (a retry of an already-committed handshake), so the caller does
    not report a duplicate contact or a duplicate introduction announcement."""
    derived = await connection.voucher_derive_stream(voucher=voucher)

    pv = persistent.PendingVoucher(
        role="inductor", conversation_id=conversation_id, step=STEP_INDUCTING,
        voucher=voucher,
        voucher_write_cap=derived.voucher_write_cap,
        voucher_read_cap=derived.voucher_read_cap,
        peer_name=peer_name,
    )
    async with persistent.asession() as sess:
        sess.add(pv)
        await sess.commit()
        await sess.refresh(pv)
        pv_id = pv.id

    voucher_payload, box1_index = await _read_box(
        connection, derived.voucher_read_cap, derived.voucher_read_cap[-_INDEX_LEN:],
        stage="derive_read_and_induct(box0)",
    )
    logger.debug(
        "derive_read_and_induct: read box0 payload on derived read_cap %s; "
        "box1 write index %s",
        _brief(derived.voucher_read_cap), _brief(box1_index),
    )

    who_reply = await _build_who_reply(conversation_id)
    induct = await connection.voucher_induct(
        voucher=voucher, voucher_payload=voucher_payload, who_reply=who_reply.to_cbor(),
    )

    await _publish_box(connection, derived.voucher_write_cap, box1_index, induct.sealed_reply)

    joiner_name = induct.display_name or peer_name
    already_inducted = False
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        if not await persistent.peer_has_read_cap(
            sess, conversation_id, induct.mutated_message_read_cap,
        ):
            _add_peer(sess, conv, joiner_name, induct.mutated_message_read_cap)
        else:
            # Already inducted (a failed post-commit ack made a naive retry
            # re-run the handshake); re-adding would duplicate the member
            # and read their stream twice.
            already_inducted = True
            logger.warning(
                "derive_read_and_induct: %r already holds read cap %s on "
                "conversation %d; skipping duplicate induction",
                joiner_name, _brief(induct.mutated_message_read_cap),
                conversation_id,
            )
        row = await sess.get(persistent.PendingVoucher, pv_id)
        await sess.delete(row)
        await sess.commit()

    if already_inducted:
        return None

    await send_introduction_message(
        conversation_id, joiner_name, induct.mutated_message_read_cap,
    )
    return joiner_name


async def _build_who_reply(conversation_id: int) -> models.GroupChatReplyWho:
    """The existing members' read caps the joiner needs to read the group: the
    inductor's own stream plus any already-active peers."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        own_read_cap = await persistent.own_read_cap(sess, conv)
        please_adds = []
        if own_read_cap is not None:
            please_adds.append(models.GroupChatPleaseAdd(
                display_name=conv.own_peer.name, read_cap=own_read_cap,
            ))
        else:
            # Neither own_rcw.read_cap nor a provisioned write cap exists
            # yet (the background provisioning loop hasn't caught up).
            # Sending a broken entry would crash the joiner's _add_peer on
            # the read_cap slice; omit ourselves instead of risking that.
            logger.warning(
                "_build_who_reply: own read cap for conversation %d is not "
                "provisioned yet; omitting self from the who-reply",
                conversation_id,
            )
        for peer in conv.peers:
            if not peer.active or peer.id == conv.own_peer_id:
                continue
            rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is not None and rcw.read_cap is not None:
                please_adds.append(models.GroupChatPleaseAdd(
                    display_name=peer.name, read_cap=rcw.read_cap,
                ))
    return models.GroupChatReplyWho(please_adds=please_adds)
