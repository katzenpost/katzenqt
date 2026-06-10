import secrets
import katzenpost_thinclient
from katzenpost_thinclient import (
    ThinClient, ThinClientOfflineError,
    BACAPDecryptionFailedError, StartResendingCancelledError,
)
from katzenpost_thinclient import Config as ThinClientConfig
import hashlib
import importlib.resources
import os
import struct
import types
import nacl.public
import secrets
import random
import logging
# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio
import traceback
import uuid
from asyncio import ensure_future
from pathlib import Path

import cbor2

from .katzen_util import create_task
from pydantic.dataclasses import dataclass
from . import conversation_handlers, models, persistent
from sqlmodel import select

logger = logging.getLogger("katzen.network")

conversation_update_queue: "Tuple[int,bool]" = asyncio.Queue()  # queue of `int`,which are Conversation.id, when we have written to ConversationLog. the bool is "redraw_only"; when True it only redraws and doesn't grow the model

__resend_queue: "Set[uuid.UUID]" = set()  # tracks bacap_streams currently in MixWAL
__resend_queue_populated = asyncio.Event() # set after existing MixWAL loaded from disk

#__plaintextwal_updated = asyncio.Event()
#__plaintextwal_updated.set()
readables_to_mixwal_event = asyncio.Event()
readables_to_mixwal_event.set()
async def signal_readables_to_mixwal():
    readables_to_mixwal_event.set()
resendable_event = asyncio.Event()  # signals send_resendable_plaintexts to check if it can do something
resendable_event.set()
async def check_for_new():
    resendable_event.set()

__mixwal_updated = asyncio.Event()
__mixwal_updated.set()
__mixnet_connected = asyncio.Event()

__on_message_queues: "Dict[bytes, asyncio.Queue]" = {}

__should_quit = asyncio.Event()
def shutdown():
    __should_quit.set()

async def start_background_threads(connection: ThinClient):
    """This should be called on startup, after establishing a connection to the mixnet.
    It runs forever.
    """
    # Loop over our write caps and retrive read caps for them:
    f1 = ensure_future(asyncio.gather(provision_read_caps(connection)))

    await __mixnet_connected.wait()

    # Loop over outgoing queue and start transmitting them to the network. Runs forever.
    f3 = ensure_future(asyncio.gather(drain_mixwal(connection)))

    # Loop over PlaintextWAL messages, encrypt them, and put them in MixWAL. Runs forever.
    f4 = ensure_future(asyncio.gather(send_resendable_plaintexts(connection)))

    # Loop over things we can read and start reading them:
    f5 = asyncio.gather(create_task(readables_to_mixwal(connection)))
    async def do_shutdown():
        """TODO this needs some work"""
        await __should_quit.wait()
        logger.info("shutting down")
        f1.cancel()
        f3.cancel()
        f4.cancel()
        f5.cancel()
    shutdown_task = create_task(do_shutdown())
    try:
        for task in asyncio.as_completed([f1, f3, f4, f5, shutdown_task]):
            try:
              await asyncio.gather(task)
            except asyncio.exceptions.CancelledError:
              logger.info(f"cancelled: {task}")
            logger.debug("start_background_threads completed: %s", task)
    except Exception as xx:  # pragma: no cover - defensive: gathered tasks catch their own
        logger.critical("f1-f4-f5 exception: %s", xx)
        raise

async def drain_mixwal(connection: ThinClient):
    try:
        await drain_mixwal2(connection) # todo why the fuck does this not catch ?
    except Exception as e:  # pragma: no cover - defensive: drain_mixwal2 handles its own errors
        logger.critical("drain_mixwal: exception: %s", e)
        import traceback
        traceback.print_exc()


async def drain_mixwal_write_single(connection:ThinClient, mw: persistent.MixWAL, draining_right_now: "set[uuid.UUID]") -> None:
    """Resend a write until it is ACK'ed by courier.

    TODO: we do not handle losing the connection to the thin client very gracefully at all here.
    """
    mw_current_idx = await connection.get_message_box_index_counter(mw.current_message_index)
    logger.info(f"TX_SINGLE idx:{mw_current_idx} ")
    from sqlmodel import select
    async with persistent.asession() as sess:
        wcw = (await sess.exec(select(persistent.WriteCapWAL).where(persistent.WriteCapWAL.id==mw.bacap_stream))).one()
    try:
      resp = await connection.start_resending_encrypted_message(
      write_cap=wcw.write_cap,
      envelope_descriptor=mw.envelope_descriptor, envelope_hash=mw.envelope_hash,
      message_ciphertext=mw.encrypted_payload,
      read_cap=None, message_box_index=None, reply_index=None

      )
    except (ThinClientOfflineError, BrokenPipeError, StartResendingCancelledError):
      logger.warning("thin client is offline or resend cancelled, can't drain mixwal.")
      return

    logger.info(f"drain_mixwal_write_single got resp: {resp}")

    """if there's no error:
    - add to Sentlog so we stop resending,
    - remove from MixWAL,
    - remove from PlaintextWAL?
    - bump send_resendable,
    - bump drain_mixwal
    """
    conv_id = await asyncio.shield(persistent.SentLog.mark_sent(connection, mw, __resend_queue))
    draining_right_now.discard(mw.bacap_stream)  # ready to send
    resendable_event.set()  # signal send_resendable_plaintexts
    __mixwal_updated.set()  # ought to be set
    if conv_id:
        # update the UX:
        create_task(conversation_update_queue.put((conv_id, True)))

_SUBSTREAM_NAME_PREFIX = ":substream:"

# Cap on attachment size after reassembly. Anything larger is
# logged at WARNING, the bytes are discarded, and a
# ``file_oversized`` marker is committed in place of a real
# ``file_marker``. The cap was set in consultation with the
# operator; it is generous enough for photos, audio clips, and
# short documents.
_ATTACHMENT_HARD_CAP = 200 * 1024 * 1024


def _attachments_root() -> Path:
    """Directory under the state file's parent where assembled
    attachments are spilled."""
    return persistent.state_file.parent / "attachments"


def _safe_basename(name: str) -> str:
    """Strip path separators and leading dots, clamp to 200 chars."""
    cleaned = (name or "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    return cleaned[:200] or "unnamed"


def _spill_attachment(
    file_upload: "models.GroupChatFileUpload",
    membership_hash: bytes,
    conversation_id: int,
) -> bytes:
    """Write the attachment bytes to disk and return the CBOR marker
    that goes into ``ConversationLog.payload``.

    Files larger than :data:`_ATTACHMENT_HARD_CAP` are dropped and a
    ``file_oversized`` marker is returned instead, so the
    conversation log still records that something arrived without
    consuming the disk.
    """
    safe = _safe_basename(file_upload.basename)
    blob = file_upload.payload
    if len(blob) > _ATTACHMENT_HARD_CAP:
        logger.warning(
            "attachment of %d bytes exceeds cap %d; dropping body",
            len(blob), _ATTACHMENT_HARD_CAP,
        )
        return b"F" + cbor2.dumps({
            "v": 0,
            "kind": "file_oversized",
            "size": len(blob),
            "basename": safe,
            "filetype": file_upload.filetype,
            "membership_hash": membership_hash,
        })

    conv_dir = _attachments_root() / str(conversation_id)
    conv_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    msg_uuid = uuid.uuid4()
    filename = f"{msg_uuid}-{safe}"
    rel_path = f"attachments/{conversation_id}/{filename}"
    abs_path = persistent.state_file.parent / rel_path

    fd = os.open(
        str(abs_path),
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)

    sha = hashlib.sha256(blob).digest()
    marker = cbor2.dumps({
        "v": 0,
        "kind": "file_marker",
        "basename": safe,
        "filetype": file_upload.filetype,
        "size": len(blob),
        "rel_path": rel_path,
        "sha256": sha,
        "membership_hash": membership_hash,
    })
    return b"F" + marker


async def _get_received_piece(sess, rcw_id: "uuid.UUID", idx_8b: bytes):
    """Lookup helper for the (read_cap, bacap_index) composite primary
    key. Returns ``None`` when no row matches."""
    return (await sess.exec(
        select(persistent.ReceivedPiece).where(
            persistent.ReceivedPiece.read_cap == rcw_id,
            persistent.ReceivedPiece.bacap_index == idx_8b,
        )
    )).first()


async def _try_assemble(sess, rcw_id: "uuid.UUID", terminal_idx_8b: bytes):
    """Walk back from a freshly-inserted ``ReceivedPiece`` and try to
    coalesce a chain.

    Returns:

    * ``("F", chunks, chain, gcm)`` when ``terminal_idx_8b`` resolves
      to a ``b'F'`` chunk and every predecessor down to the substream's
      start (or the previous ``b'F'``/``b'I'`` boundary) is present
      and the concatenated CBOR decodes to a :class:`GroupChatMessage`.
      ``chunks`` is the ordered ``(type, body)`` list passed to
      :func:`models.unserialize`; ``chain`` is the ordered list of
      ``ReceivedPiece`` rows the caller may delete on commit.
    * ``("I", read_cap_bytes, [piece])`` when ``terminal_idx_8b``
      resolves to a ``b'I'`` chunk, whose body is the substream's
      read cap.
    * ``None`` when the chain is still open (no terminator) or has
      gaps that prevent a clean decode.

    BACAP indices are 64-bit little-endian counters stored in the
    first eight bytes of ``MessageBoxIndex`` blobs; predecessors
    are at ``counter - 1``.
    """
    cur = await _get_received_piece(sess, rcw_id, terminal_idx_8b)
    if cur is None:
        return None
    if cur.chunk_type == b"I":
        return ("I", cur.chunk, [cur])
    if cur.chunk_type != b"F":
        return None  # a lone 'C', chain not yet terminated

    chain = [cur]
    counter = int.from_bytes(terminal_idx_8b, "little")
    while counter > 0:
        counter -= 1
        prev = await _get_received_piece(
            sess, rcw_id, counter.to_bytes(8, "little"),
        )
        if prev is None:
            break  # either gap or substream start; let CBOR decode decide
        if prev.chunk_type in (b"F", b"I"):
            break  # boundary with a prior assembled message
        chain.insert(0, prev)

    chunks = [(rp.chunk_type, rp.chunk) for rp in chain]
    try:
        gcm = models.unserialize(chunks)
    except Exception as exc:  # malformed CBOR or framing: leave RPs for retry
        logger.warning(
            "could not assemble chain at rcw=%s terminal=%s: %s",
            rcw_id, terminal_idx_8b.hex(), exc,
        )
        return None
    if gcm is None:
        return None
    return ("F", chunks, chain, gcm)


async def drain_mixwal_read_single(*, connection:ThinClient, rcw_read_cap: bytes, mw: persistent.MixWAL, draining_right_now: "set[uuid.UUID]"):
  """Given a single persisten.MixWAL with is_read==True:
    - Send it to the network.
    - If we get a response:
      - A message: We can progress
      - A box not found:
        - We should: Resend at at later time (handled by kpclientd)
  """
  assert mw.is_read
  assert len(rcw_read_cap) == 136
  bacap_uuid = mw.bacap_stream

  def give_up() -> None:
    """Unblocks the mw so it can be scheduled again."""
    draining_right_now.discard(bacap_uuid)
    # we don't clear it from __resend_queue because we don't want to skip
    # ahead in the stream.
    readables_to_mixwal_event.set()
    return

  # we should check that mw.destination exists:
  if not courier_destination_exists(connection, mw.destination):
      logger.error("outbound read mw for courier that no longer exists")
      await asyncio.sleep(500)  # we want to wait until next PKI doc
      give_up()
      return

  try:
    resp = await connection.start_resending_encrypted_message(
        read_cap=rcw_read_cap,
        write_cap=None,
        message_box_index=mw.current_message_index,
        reply_index=None,
        envelope_descriptor=mw.envelope_descriptor,
        envelope_hash=mw.envelope_hash,
        message_ciphertext=mw.encrypted_payload,
        no_retry_on_box_id_not_found=False,
   )
  except (katzenpost_thinclient.core.MKEMDecryptionFailedError,
          BACAPDecryptionFailedError, StartResendingCancelledError,
          ThinClientOfflineError, BrokenPipeError) as e:
    logger.warning("drain_mixwal_read_single giving up: %s", e)
    await asyncio.sleep(5)
    give_up()
    return

  logger.debug(f"got reply for outbound read mw {resp}")
  assert resp is not None, "outbound read reply is None, but ought to be retrying"
  async with persistent.asession() as sess:
    rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
    idx_old = await connection.get_message_box_index_counter(rcw.next_index)
    idx_new = await connection.get_message_box_index_counter(mw.next_message_index)
    if idx_old >= idx_new:
      logger.warning(f"not advancing idx to {idx_new} from old {idx_old}, we probably already handled this? ought to not be possible.")
      try:
        await sess.delete(mw)
        await sess.commit()
      except Exception as e:  # pragma: no cover - defensive: commit-of-delete should never fail
        logger.critical("error committing deletion of stray MW: %s", e)
      readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
      return
    logger.info(f"advancing read to idx {idx_new}")
    assert idx_new == idx_old + 1, f"idx mismatch {idx_new} != {idx_old} + 1"
    rcw.next_index = mw.next_message_index
    sess.add(rcw)
    chunk_type = resp.plaintext[:1]
    chunk_body = resp.plaintext[1:]
    if chunk_type not in (b"F", b"C", b"I"):
      logger.critical(f"received message with invalid prefix, going to stop reading this peer {resp}")
      cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
      cp.active = False
      sess.add(cp)
      await sess.delete(mw)
      await sess.commit()
      __mixwal_updated.set()
      return

    cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
    sess.add(persistent.ReceivedPiece(
                read_cap=mw.bacap_stream,
                bacap_index=mw.current_message_index[:8],
                chunk_type=chunk_type,
                chunk=chunk_body,
            ))

    assembled = await _try_assemble(
        sess, mw.bacap_stream, mw.current_message_index[:8],
    )
    convlog_added = False
    signal_send = False
    notify_conv_id = cp.conversation.id

    if assembled is not None and assembled[0] == "F":
      _, chunks, chain, gcm = assembled
      if gcm.file_upload is not None:
        target_conv_id = (
            (await sess.get(persistent.ConversationPeer, int(cp.name.split(":")[2]))).conversation.id
            if cp.name.startswith(_SUBSTREAM_NAME_PREFIX)
            else cp.conversation.id
        )
        full_payload = _spill_attachment(
            gcm.file_upload, gcm.membership_hash, target_conv_id,
        )
      else:
        full_payload = b"F" + b"".join(body for _kind, body in chunks)
      if cp.name.startswith(_SUBSTREAM_NAME_PREFIX):
        # Substream's terminal F: commit the assembled message into the
        # parent peer's ConversationLog, prune the parent's indirection
        # piece, and retire this synthetic peer.
        parent_id = int(cp.name.split(":")[2])
        parent_peer = await sess.get(persistent.ConversationPeer, parent_id)
        added, sig = await conversation_handlers.dispatch(sess, parent_peer, gcm, full_payload)
        signal_send = signal_send or sig
        parent_i = (await sess.exec(
            select(persistent.ReceivedPiece).where(
                persistent.ReceivedPiece.read_cap == parent_peer.read_cap_id,
                persistent.ReceivedPiece.chunk_type == b"I",
                persistent.ReceivedPiece.chunk == rcw.read_cap,
            )
        )).first()
        if parent_i is not None:
          await sess.delete(parent_i)
        cp.active = False
        sess.add(cp)
        notify_conv_id = parent_peer.conversation.id
        convlog_added = added
      else:
        # Top-level F (single-box or contiguous on the parent stream): route
        # by message type, chat into the log, tally into the controller.
        convlog_added, sig = await conversation_handlers.dispatch(sess, cp, gcm, full_payload)
        signal_send = signal_send or sig
      for rp in chain:
        await sess.delete(rp)

    elif assembled is not None and assembled[0] == "I":
      _, substream_read_cap, _ = assembled
      if len(substream_read_cap) == 136:
        new_rcw = persistent.ReadCapWAL(
            id=uuid.uuid4(),
            read_cap=substream_read_cap,
            next_index=substream_read_cap[-104:],
        )
        sess.add(new_rcw)
        substream_peer = persistent.ConversationPeer(
            name=f"{_SUBSTREAM_NAME_PREFIX}{cp.id}:{secrets.token_hex(2)}",
            read_cap_id=new_rcw.id,
            active=True,
            conversation=cp.conversation,
        )
        sess.add(substream_peer)
      else:
        logger.warning(
            "ignoring indirection with malformed read cap length %d",
            len(substream_read_cap),
        )

    await sess.delete(mw)
    bacap_uuid = mw.bacap_stream
    await sess.commit()

  if convlog_added:
    create_task(conversation_update_queue.put((notify_conv_id, False)))

  if signal_send:
    # A tally sync request staged a reply on the outgoing stream; poke the
    # send loop now that the receive transaction has committed.
    await check_for_new()

  draining_right_now.discard(bacap_uuid)
  __resend_queue.discard(bacap_uuid)  # this should be .remove(), but why is it empty?
  readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
  __mixwal_updated.set()


async def _wait_for_connection_or_shutdown() -> bool:
    """Block until either __mixnet_connected is set or __should_quit fires.

    Returns True if the mixnet is reportedly connected, False if shutdown
    happened first. Drain loops use this at the top of each iteration so
    they pause cleanly during outages instead of burning cycles against
    a daemon that will only raise ThinClientOfflineError back at them.
    """
    if __should_quit.is_set():
        return False
    if __mixnet_connected.is_set():
        return True
    await asyncio.wait((
        create_task(__mixnet_connected.wait()),
        create_task(__should_quit.wait()),
    ), return_when=asyncio.FIRST_COMPLETED)
    return __mixnet_connected.is_set() and not __should_quit.is_set()


async def drain_mixwal2(connection: ThinClient):
    """Read from MixWAL and put the messages on the network."""
    """"Send messages to mixnet from MixWAL.

    - Listen for new entries in MixWAL
    - for each envelope_hash:
      - if we don't have a resend state for it, create it
      - ThinClient.send_message
      - start timer that resends it
      - if a reply from courier comes in:
        - delete timer
        - delete from MixWAL
        - bump MessageBoxIndex
    """

    draining_right_now : "Set[uuid.UUID]" = set()
    await __resend_queue_populated.wait()
    shutdown = create_task(__should_quit.wait())
    while not __should_quit.is_set():
        # Pause while the mixnet is unreachable. on_connection_status
        # toggles __mixnet_connected so a kpclientd reconnect (or an
        # outage and recovery on the wire) resumes us cleanly.
        if not await _wait_for_connection_or_shutdown():
            continue
        # asyncio.wait defaults to ALL_COMPLETED, which would force this
        # loop to wait the full timeout (or for shutdown) regardless of
        # __mixwal_updated being set, effectively turning it into a
        # 15-second poller. FIRST_COMPLETED restores the event-driven
        # behaviour the caller of __mixwal_updated.set() expects.
        _, _ = await asyncio.wait((
                 create_task(__mixwal_updated.wait()),
                 shutdown,
        ), timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if __should_quit.is_set():
          continue
        __mixwal_updated.clear()
        logger.debug("DRAIN_MIXWAL draining_right_now:%s __resend_queue:%s", draining_right_now, __resend_queue)
        # TODO drain new from mixwal, this should NOT be a long-running session like it currently is
        new_write_mws = []
        async with persistent.asession() as sess:
            new_mixwals = (await sess.exec(persistent.MixWAL.get_new(draining_right_now))).all()
            for mw in new_mixwals:
                if mw.is_read:
                    draining_right_now.add(mw.bacap_stream)
                    __resend_queue.add(mw.bacap_stream)
                    rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
                    if len(rcw.read_cap) != 136:
                      raise Exception(f"ReadCapWAL.rcw from persistent has incorrect size: len(rcw.read_cap) {repr(rcw)} (from {mw.bacap_stream}")
                    read_task = create_task(drain_mixwal_read_single(connection=connection, rcw_read_cap=rcw.read_cap, mw=mw, draining_right_now=draining_right_now))
                    read_task.add_done_callback(lambda task: readables_to_mixwal_event.set())
                else:
                    new_write_mws.append(mw)
        for mw in new_write_mws:
            if not courier_destination_exists(connection, mw.destination):
                logger.warning("mw courier is currently not in PKI")
                continue
            logger.debug("drain_mixwal: NEW (write) MIXWAL idx=%s is_read=%s bacap_stream=%s",
                         await connection.get_message_box_index_counter(mw.current_message_index),
                         mw.is_read, mw.bacap_stream)
            draining_right_now.add(mw.bacap_stream) # this is the uuid PK
            __resend_queue.add(mw.bacap_stream)  # ensure readables_to_mixwal() does not serialize new ones for this stream
            write_task = create_task(drain_mixwal_write_single(connection, mw, draining_right_now))


async def provision_read_caps(connection: ThinClient):
    """Long-running process to tread persistent.WriteCapWAL and populate ReadCapWAL"""
    #print("provision read caps"*100)
    import sqlalchemy as sa
    wait = 0
    while not __should_quit.is_set():
        await asyncio.sleep(wait)  # could make this smoother with an asyncio.Event(), but 5s is fine for now.
        wait = 5
        async with persistent.asession() as sess:
            for (rcw, wcw) in await sess.exec(sa.select(persistent.ReadCapWAL,persistent.WriteCapWAL).where(persistent.ReadCapWAL.read_cap == None).where(persistent.ReadCapWAL.write_cap_id==persistent.WriteCapWAL.id)): #  &
                logger.debug("provision_read_caps UPDATING rcw=%s wcw=%s write_cap=%s next_index=%s",
                             rcw, wcw, wcw.write_cap, wcw.next_index)
                if wcw.write_cap is None:
                    try:
                        keypair_res = await connection.new_keypair(seed=secrets.token_bytes(32))
                    except Exception as e:
                        logger.warning("new_keypair did not work: %s", e)
                        continue
                    wcw.write_cap = keypair_res.write_cap
                    wcw.next_index = keypair_res.first_message_index
                    rcw.read_cap = keypair_res.read_cap
                    rcw.next_index = keypair_res.first_message_index
                    sess.add(wcw)
                    sess.add(rcw)
                    await sess.commit()
                    resendable_event.set() # start sending PlaintextWAL msgs that were waiting on this WriteCap
                    readables_to_mixwal_event.set() # start reading the ReadCap if it's active
                    continue
                else:
                    logger.warning("DB was created with old API; new API does not support converting write cap to read cap")
                    continue
            await sess.commit()

async def readables_to_mixwal(connection):
    """
    Look up all of our read caps, start sending reads for all the "active" ones that we
    aren't currently trying to read.
    """
    logger.debug("readables_to_mixwal: starting")
    global __resend_queue
    await __resend_queue_populated.wait()
    async def process_box(cpeer:persistent.ConversationPeer, rcw:persistent.ReadCapWAL) -> persistent.MixWAL:
        logger.debug("process box cpeer-rcw:", cpeer, await connection.get_message_box_index_counter(rcw.next_index))
        rcreply: "EncryptReadResult" = await connection.encrypt_read(read_cap=rcw.read_cap, message_box_index=rcw.next_index)
        logger.debug("process_box got this from encrypt_read: %s", rcreply)
        courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
        mw = persistent.MixWAL(
            bacap_stream=rcw.id,
            plaintextwal=None,
            envelope_hash=rcreply.envelope_hash,
            destination=courier,
            encrypted_payload=rcreply.message_ciphertext,
            envelope_descriptor=rcreply.envelope_descriptor,
            next_message_index=rcreply.next_message_box_index,
            current_message_index=rcw.next_index,
            # rcreply.reply_index
            is_read=True,
        )
        return mw
    while not __should_quit.is_set():
        # Pause while the mixnet is unreachable so the loop does not
        # try to encrypt_read against a daemon that cannot route.
        if not await _wait_for_connection_or_shutdown():
            continue
        logger.debug("SLEEPING FOR READABLES_TO_MIXWAL"*2)
        a, b = await asyncio.wait([create_task(readables_to_mixwal_event.wait())], timeout=60)
        if __should_quit.is_set():
            continue
        if not len(a):
            logger.debug("readables_to_mixwal_event.wait() timed out, nothing new to read")
            continue
        readables_to_mixwal_event.clear()
        logger.debug("IN READABLES_TO_MIXWAL_LOOP")
        async with persistent.asession() as sess:
            # TODO are these guaranteed to be distinct?
            readable_peers = (await sess.exec(select(
                persistent.ConversationPeer, persistent.ReadCapWAL
            ).where(persistent.ConversationPeer.active==True
                    ).where(persistent.ConversationPeer.read_cap_id == persistent.ReadCapWAL.id
                            ).where(
                                persistent.ReadCapWAL.id.not_in(select(persistent.MixWAL.bacap_stream)) # todo does the rcw.id correspond to a bacap_stream?? should we use the same id for both?
                            )
                )
            ).all()
            logger.debug("readable_peers: %d", len(readable_peers))
            for (cpeer, rcw) in readable_peers:
                logger.debug("going to process_box", cpeer.name, rcw.next_index[:8].hex())
                try:
                  mw = await process_box(cpeer, rcw)
                except Exception as e:
                  logger.critical(f"process_box failed: {e}")
                  continue
                sess.add(mw)
                logger.debug("finished one peer: %s", cpeer.name)
            logger.debug("readables_to_mixwal: committing")
            await sess.commit()
        logger.debug("done readables_to_mixwal: %d peers", len(readable_peers))
        if len(readable_peers):
            __mixwal_updated.set()
            logger.debug("__mixwal_updated.set() from readables_to_mixwal")

def on_error(task, func, *args, **kwargs):
    """calls func(*args,**kwargs) if task has an exception.
    Usage: task.add_done_callback(on_error(lambda: foo.bar()))
    """
    def on_error_done(task):
        try:
            task.result()
        except Exception:
            func(*args, **kwargs)
            raise
    task.add_done_callback(on_error_done)
    return task

async def send_resendable_plaintexts(connection:ThinClient) -> None:
    # look at persistent.PlaintextWAL:
    # PICK OUT stuff in PlaintextWAL that we aren't currently resending
    # - mark them as "being resent" (in memory)
    # - when we get an ACK, we move it from being resent to "sent" (db),
    #   and remove it from the PlaintextWAL, atomically
    global __resend_queue
    __resend_queue |= await persistent.MixWAL.resend_queue_from_disk()
    __resend_queue_populated.set()
    while not __should_quit.is_set():
        # Pause while the mixnet is unreachable; encrypt_write/
        # start_resending need a live route through kpclientd.
        if not await _wait_for_connection_or_shutdown():
            continue
        _, _ = await asyncio.wait((create_task(resendable_event.wait()),), timeout=60)
        if __should_quit.is_set():
            continue
        resendable_event.clear()
        logger.debug("send_resendable_plaintexts: running")
        pwals_to_send = set()
        async with persistent.asession() as sess:
            query = persistent.PlaintextWAL.find_resendable(__resend_queue)
            sendable_rows = (await sess.exec(query)).all()
            # find_resendable wraps PlaintextWAL inside a row_number()
            # subquery and returns plain Row tuples that are not
            # ORM-tracked. For an indirection PWAL we re-fetch the
            # tracked instance to fill in its bacap_payload, then
            # snapshot the (id, bacap_stream, payload) we need for
            # dispatch into a detachable container so start_resending
            # never touches a session-bound attribute.
            dispatch: "list[types.SimpleNamespace]" = []
            for row in sendable_rows:
                payload = row.bacap_payload
                if row.indirection is not None and not payload:
                    # find_resendable only surfaces indirection PWALs
                    # whose target ReadCapWAL has been provisioned
                    # (read_cap IS NOT NULL), so the get is guaranteed
                    # to find a populated read_cap here.
                    logger.debug(
                        "filling indirection PWAL %s from rcw %s",
                        row.id, row.indirection,
                    )
                    rcw = await sess.get(persistent.ReadCapWAL, row.indirection)
                    payload = b'I' + rcw.read_cap
                    pwal_orm = await sess.get(persistent.PlaintextWAL, row.id)
                    if pwal_orm is not None:
                        pwal_orm.bacap_payload = payload
                        sess.add(pwal_orm)
                dispatch.append(types.SimpleNamespace(
                    id=row.id,
                    bacap_stream=row.bacap_stream,
                    bacap_payload=payload,
                ))
            await sess.commit()
        for pwal in dispatch:
            if pwal.bacap_stream not in __resend_queue:
                __resend_queue.add(pwal.bacap_stream)
                t = create_task(start_resending(connection, pwal))
                # Default-arg capture pins pwal.bacap_stream at lambda
                # creation time. The prior `lambda: ... pwal.bacap_stream`
                # closed over the loop variable and, on an inner iteration's
                # failure, discarded the LAST iteration's bacap_stream,
                # stranding the actual failer in __resend_queue forever.
                on_error(t, lambda s=pwal.bacap_stream: __resend_queue.discard(s))  # when cancelled/exception

async def start_resending(connection:ThinClient, pwal: persistent.PlaintextWAL):
    """
    called by network:send_resumable_plaintexts, at startup and peridically, guarded by __resend_queue
    creates MixWAL entries for plaintexts.

    Given a PlaintextWAL entry (plaintext bacap_payload, bacap_stream uuid)
    we need to call:
    - create_write_channel() ->     alice_channel_id, read_cap, write_cap = await alice_thin_client.create_write_channel()
      - persist to bacap_stream_uuid -> read_cap/write_cap
        - what do we do about next_index? we can recover it from the write_cap
        - that lets us call resume_write_channel(write_cap, message_box_index=write_cap[FOO:])
    - write_reply = await alice_thin_client.write_channel(alice_channel_id, pwal.bacap_payload)
      - this give us a WriteChannelReply containing:
        send_message_payload
        current_message_index
        next_message_index
        envelope_descriptor
        envelope_hash
      - these we persist to MixWAL
    """
    async with persistent.asession() as sess:
        wc: persistent.WriteCapWAL = await sess.get(persistent.WriteCapWAL, pwal.bacap_stream)

    # now we have:
    # - a plaintext to send to send, pwal.bacap_payload
    # - wc has .write_cap, .next_index
    # and we need to:
    # - pick a courier
    # - encrypt the message
    # - persist that to MixWAL

    wcr : "EncryptWriteResult" = await connection.encrypt_write(write_cap=wc.write_cap,
          message_box_index=wc.next_index,
          plaintext=pwal.bacap_payload)

    next_message_index = wcr.next_message_box_index

    courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    mw = persistent.MixWAL(
        bacap_stream=pwal.bacap_stream,
        plaintextwal=pwal.id,
        envelope_hash = wcr.envelope_hash,
        destination = courier,  # TODO not used anywhere
        encrypted_payload = wcr.message_ciphertext,
        envelope_descriptor = wcr.envelope_descriptor,
        current_message_index = wc.next_index,
        next_message_index = next_message_index, # for resends, do we need current_message_index ?
        is_read = False
    )
    async with persistent.asession() as sess:
        sess.add(mw)
        # TODO if we do this, how do we know what to remove from PlaintextWAL?
        # we need to hook network.on_message_reply to look for the returns
        await sess.commit()
    # Now we have persisted our intention to resend.
    # Next up is something needs to actually resend, reading from MixWAL
    # and issuing ThinClient.start_resending_encrypted_message
    __mixwal_updated.set()

async def on_connection_status(status:"Dict[str,Any]"):
    if status["is_connected"]:
      __mixnet_connected.set()
    else:
      __mixnet_connected.clear()
    if status["err"] or status.get("Err", None):
        logger.error("ON_CONNECTION_STATUS err: %s", status)
        #ON_CONNECTION_STATUS err: {'is_connected': False, 'err': {'Op': 'read', 'Net': 'tcp', 'Source': {'IP': b'\x7f\x00\x00\x01', 'Port': 51718, 'Zone': ''}, 'Addr': {'IP': b'\x7f\x00\x00\x01', 'Port': 30004, 'Zone': ''}, 'Err': {}}}
        # why is clientd telling us about the IP addresses its trying to connect to?
        # and why does it have both status['err'] and status['Err']?
        #import pdb;pdb.set_trace()
        return

async def on_message_reply(reply):
    """Gets called each time a message reply comes in, whether it's from
    something we wrote or read.
    TODO pretty annoying that it's not async ...
    """
    # Receives something like:
    # {'message_id': b'\n\x90\xc2\x0cr\xa8\xa2+\x17Y\xcb\x837\xcc\x0f\x9b', 'surbid': None, 'payload': None}
    if async_queue := __on_message_queues.get(reply['message_id'], None):
        logger.debug("on_message_reply: matched queue for message_id=%s payload=%s",
                     reply['message_id'].hex(), reply['payload'])
        create_task(async_queue.put(reply))
        logger.debug("on_message_reply: enqueued message_id=%s", reply['message_id'].hex())
    else:
        logger.debug("on_message_reply: no queue match, reply=%s", reply)
    return
    # TODO wait for ACK, then:
    # once reply comes in:
    """
    async with persistent.asession() as sess:
        #   listener should put uuid in SentLog
        sess.add(persistent.SentLog(pwal.id))
        #   listener should remove from PlaintextWAL
        sess.delete(pwal)
        #   resend envelope deleted from MixWAL:
        sess.delete(mw)
        await sess.commit()
    """

async def on_message_sent(reply):
    """Example:
    {'message_id': b'\xe1\xb85\xe8u]\xf8\x85\xa9\xa7\xac\xf7\xcc\xe6\xdfQ',
    'surbid': b'\xf3\xa1\xfdni\r2\xe9\xbalH\xcfK\x89\x8e\xee',
    'sent_at': 1751741438,
    'reply_eta': 0,  # TODO we should make the resend try to match the reply_eta
    'err': 'client/conn: PKI error: client2: failed to find destination service node: pki: service not found'}
    """
    if err := reply.get('err', None):
        logger.error("ERR for outgoing message_id=%s: %s", reply['message_id'].hex(), err)
    else:
        logger.debug("MESSAGE SENT OK: message_id=%s reply=%s", reply['message_id'].hex(), reply)

def resolve_thinclient_config(explicit: "str | Path | None" = None) -> Path:
    """Locate ``thinclient.toml`` using a precedence chain.

    The chain is consulted in order, returning the first path that
    exists on disk:

      1. ``explicit`` argument (raises ``FileNotFoundError`` if given
         and missing; the caller asked for this file specifically),
      2. ``$KATZENQT_THINCLIENT_CONFIG``,
      3. ``$XDG_CONFIG_HOME/katzenqt/thinclient.toml`` (default
         ``~/.config/katzenqt/thinclient.toml``),
      4. the bundled copy shipped under ``katzenqt/data/thinclient.toml``
         (resolved via ``importlib.resources``),
      5. the development-tree fallback at
         ``<repo>/config/thinclient.toml``.
    """
    if explicit is not None:
        explicit_path = Path(explicit)
        if not explicit_path.is_file():
            raise FileNotFoundError(
                f"thinclient config not found at explicit path: {explicit_path}"
            )
        return explicit_path

    candidates: "list[Path]" = []
    env = os.environ.get("KATZENQT_THINCLIENT_CONFIG")
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates.append(Path(xdg) / "katzenqt" / "thinclient.toml")
    try:
        bundled = importlib.resources.files("katzenqt") / "data" / "thinclient.toml"
        candidates.append(Path(str(bundled)))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    candidates.append(
        Path(__file__).resolve().parent.parent.parent / "config" / "thinclient.toml"
    )

    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Could not locate thinclient.toml in: "
        + ", ".join(str(c) for c in candidates)
    )


# from katzenpost_thinclient import ThinClient, Config
async def reconnect(config_path: "str | Path | None" = None) -> ThinClient:
    resolved = resolve_thinclient_config(config_path)
    cfg = ThinClientConfig(
        str(resolved),
        on_message_reply=on_message_reply,
        on_message_sent=on_message_sent,
        on_connection_status=on_connection_status,
        #on_new_pki_document=...
    )
    client = ThinClient(cfg)
    await client.start(asyncio.get_running_loop())  # this can throw exceptions
    return client

# events: we should keep track of ConnectionStatusEvent.IsConnected so we can tell the user whether the mixnet client is working

# ThinClient.send_message(surb_id, payload, dest_node, dest_queue)

def create_new_keypair(seed: bytes):
    """Makes a new WriteCap/ReadCap pair from a 32byte seed, using blake2b as KDF"""
    assert len(seed) == 32
    assert isinstance(seed, bytes)
    from nacl.hash import blake2b
    from nacl.signing import SigningKey
    def gen_bytes(purpose:bytes, length:int) -> bytes:
        return blake2b(
            data=b'KP:'+purpose,
            key=seed, # IKM
            salt=b'',
            person=b'', digest_size=length, encoder=nacl.encoding.RawEncoder
        )
    start_idx_raw:bytes = gen_bytes(b'start_idx', 16)
    idx1, idx2 = struct.unpack('<2Q', start_idx_raw)
    start_idx = struct.pack('<Q', (idx1 + idx2) & 0x7fffffffffffffff)
    priv_obj = SigningKey(gen_bytes(b'signing_key', 32))
    first_message_index = start_idx + gen_bytes(b'blinding_factor', 32) + gen_bytes(b'encryption_key', 32) + gen_bytes(b'HKDF_state',32)
    assert len(first_message_index) == 104 # 8 + 32 + 32 + 32
    read_cap = priv_obj.verify_key.encode() + first_message_index
    write_cap = priv_obj.encode() + read_cap
    assert write_cap[32:] == read_cap[:]
    assert write_cap[:32] != read_cap[:32]
    assert len(write_cap) == 32 + 32 + 104
    assert len(read_cap)  == 32 + 104
    return write_cap, read_cap

def courier_destination_exists(connection, destination) -> bool:
    """Check that an old destination (hash of the IdentityKey) exists in this PKI, and that the node is a Courier.
    We should not be resending MixWAL entries whose courier has gone away.

    TODO: when ensuring courier exists we usually also want to make sure there are (some) replicas present.
    """
    try:
        couriers = connection.get_all_couriers()
    except Exception:
        return False
    return any(identity_hash == destination for identity_hash, _queue_id in couriers)

async def test_keypair(connection, write_cap, read_cap):
    """Test that create_new_keypair() results in usable+matching write/read caps."""
    courier = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    logger.debug("courier exists? %s %s", courier, courier_destination_exists(connection, courier))

    wcr = await connection.encrypt_write(
        plaintext=b'hello',
        write_cap=write_cap,
        message_box_index=write_cap[-104:])
    await connection.start_resending_encrypted_message(
        read_cap=None, write_cap=write_cap, message_box_index=None,
        reply_index=None,
        envelope_descriptor=wcr.envelope_descriptor,
        message_ciphertext=wcr.message_ciphertext,
        envelope_hash=wcr.envelope_hash)

    await asyncio.sleep(20)

    rcr = await connection.encrypt_read(
        read_cap=read_cap,
        message_box_index=read_cap[-104:])
    await connection.start_resending_encrypted_message(
        read_cap=read_cap, write_cap=None,
        message_box_index=read_cap[-104:],
        reply_index=None,
        envelope_descriptor=rcr.envelope_descriptor,
        message_ciphertext=rcr.message_ciphertext,
        envelope_hash=rcr.envelope_hash)
