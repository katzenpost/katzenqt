import secrets
import katzenpost_thinclient
from katzenpost_thinclient import ThinClient, ThinClientOfflineError
from katzenpost_thinclient import Config as ThinClientConfig
import hashlib
import cbor2
import struct
import nacl.public
import secrets
import random
import logging
# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio
import traceback
from asyncio import ensure_future
from .katzen_util import create_task
from pydantic.dataclasses import dataclass
from . import persistent
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
            logger.debug("start_background_threads completed: ",task)
    except Exception as xx:
        logger.critical("f1-f4-f5 exception",xx)
        raise

async def drain_mixwal(connection: ThinClient):
    try:
        await drain_mixwal2(connection) # todo why the fuck does this not catch ?
    except Exception as e:
        logger.critical("drain_mixwal: exception", e)
        import traceback
        traceback.print_exc()


async def drain_mixwal_write_single(connection:ThinClient, mw: persistent.MixWAL, draining_right_now: "set[uuid.UUID]") -> None:
    """Resend a write until it is ACK'ed by courier.

    TODO: we do not handle losing the connection to the thin client very gracefully at all here.
    """
    mw_current_idx = struct.unpack('<Q', mw.current_message_index[:8])[0]
    logger.info(f"TX_SINGLE idx:{mw_current_idx} ")
    from sqlmodel import select
    async with persistent.asession() as sess:
        wcw = (await sess.exec(select(persistent.WriteCapWAL).where(persistent.WriteCapWAL.id==mw.bacap_stream))).one()
    try:
      resp = await connection.start_resending_encrypted_message(
      write_cap=wcw.write_cap,
      # next_message_index=mw.current_message_index,
      envelope_descriptor=mw.envelope_descriptor, envelope_hash=mw.envelope_hash,
      message_ciphertext=mw.encrypted_payload,
      read_cap=None, next_message_index=None, reply_index=None

      )
    except (ThinClientOfflineError, BrokenPipeError):
      logger.warning("thin client is offline, can't drain mixwal.")
      return

    logger.info(f"drain_mixwal_write_single got resp: {resp}")

    """if there's no error:
    - add to Sentlog so we stop resending,
    - remove from MixWAL,
    - remove from PlaintextWAL?
    - bump send_resendable,
    - bump drain_mixwal
    """
    conv_id = await asyncio.shield(persistent.SentLog.mark_sent(mw, __resend_queue))
    draining_right_now.discard(mw.bacap_stream)  # ready to send
    resendable_event.set()  # signal send_resendable_plaintexts
    __mixwal_updated.set()  # ought to be set
    if conv_id:
        # update the UX:
        create_task(conversation_update_queue.put((conv_id, True)))

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
        next_message_index=mw.current_message_index,
        reply_index=None,
        envelope_descriptor=mw.envelope_descriptor,
        envelope_hash=mw.envelope_hash,
        message_ciphertext=mw.encrypted_payload,
        no_retry_on_box_id_not_found=False,
   )
  except katzenpost_thinclient.core.MKEMDecryptionFailedError as e:
    logger.critical(f"{e}: ")
    await asyncio.sleep(5)
    give_up()
    return

  logger.critical(f"got reply for outbound read mw {resp}")
  assert resp is not None, "outbound read reply is None, but ought to be retrying"
  async with persistent.asession() as sess:
    rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
    idx_old, idx_new = struct.unpack("<2Q", rcw.next_index[:8] + mw.next_message_index[:8])
    if idx_old >= idx_new:
      logger.warning(f"not advancing idx to {idx_new} from old {idx_old}, we probably already handled this? ought to not be possible.")
      try:
        await sess.delete(mw)
        await sess.commit()
      except Exception as e:
        logger.critical(f"error committing deletion of stray MW")
      readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
      return
    logger.info(f"advancing read to idx {idx_new}")
    assert idx_new == idx_old + 1, f"idx mismatch {idx_new} != {idx_old} + 1"
    rcw.next_index = mw.next_message_index
    sess.add(rcw)
    if resp.startswith(b"I"):
      logger.debug(f"received indirection {resp}")
      import pdb;pdb.set_trace()
      pass
    elif resp.startswith(b"F"):
      # it's a discrete message
      logger.debug(f"received Final message {resp}")
      pass
    elif resp.startswith(b"C"):
      logger.debug(f"received C message {resp}")
      pass
    else:
      logger.critical(f"received message with invalid prefix, going to stop reading this peer {resp}")
      cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
      cp.active = False
      sess.add(cp)
      await sess.delete(mw)
      await sess.commit()
      __mixwal_updated.set()
      return

    cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
    # Here we should write to the ReceivedPiece table:
    #
    sess.add(persistent.ReceivedPiece(
                read_cap=mw.bacap_stream,
                bacap_index=mw.current_message_index[:8],
                chunk_type=resp[:1],
                chunk=resp[1:]
            ))
    cl = persistent.ConversationLog.append_from(cp, resp)
    sess.add(cl)
    await sess.delete(mw)
    c_id = cp.conversation.id
    bacap_uuid = mw.bacap_stream
    await sess.commit()

  create_task(conversation_update_queue.put((c_id,False)))

  draining_right_now.discard(bacap_uuid)
  __resend_queue.discard(bacap_uuid)  # this should be .remove(), but why is it empty?
  readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
  __mixwal_updated.set()


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
    async def with_own_sess(mw):
        async with persistent.asession() as sess:
            done_with_this = await tx_single(sess, mw)
        # __resend_queue.remove(mw.bacap_stream)
    await __resend_queue_populated.wait()
    shutdown = create_task(__should_quit.wait())
    while not __should_quit.is_set():
        _, _ = await asyncio.wait((
                 create_task(__mixwal_updated.wait()),
                 shutdown,
        ), timeout=15)
        if __should_quit.is_set():
          continue
        __mixwal_updated.clear()
        print(f"DRAIN_MIXWAL draining_right_now:{draining_right_now} __resend_queue:{__resend_queue}")
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
            print("drain_mixwal: NEW (write) MIXWAL:", struct.unpack("<1Q", mw.current_message_index[:8]), mw.is_read, mw.bacap_stream)
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
                print("UPDATING:"*100, rcw,wcw, wcw.write_cap, wcw.next_index)
                if wcw.write_cap is None:
                    try:
                        keypair_res = await connection.new_keypair(seed=secrets.token_bytes(32))
                    except Exception as e:
                        logger.warn(f"new_keypair DID NOT WORK: {e}")
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
                    print("DB was created with old API, new API does not support converting write cap to read cap")
                    continue
            await sess.commit()

async def readables_to_mixwal(connection):
    """
    Look up all of our read caps, start sending reads for all the "active" ones that we
    aren't currently trying to read.
    """
    await __mixnet_connected.wait()
    print("READABLES READING")
    global __resend_queue
    await __resend_queue_populated.wait()
    async def process_box(cpeer:persistent.ConversationPeer, rcw:persistent.ReadCapWAL) -> persistent.MixWAL:
        logger.debug("process box cpeer-rcw:", cpeer, struct.unpack('<Q', rcw.next_index[:8]))
        rcreply: "EncryptReadResult" = await connection.encrypt_read(read_cap=rcw.read_cap, message_box_index=rcw.next_index)
        print("process_box got this from encrypt_read:", rcreply)
        courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
        mw = persistent.MixWAL(
            bacap_stream=rcw.id,
            plaintextwal=None,
            envelope_hash=rcreply.envelope_hash,
            destination=courier,
            encrypted_payload=rcreply.message_ciphertext,
            envelope_descriptor=rcreply.envelope_descriptor,
            next_message_index=await connection.next_message_box_index(rcw.next_index),
            current_message_index=rcw.next_index,
            # rcreply.reply_index
            is_read=True,
        )
        return mw
    while True:
        logger.debug("SLEEPING FOR READABLES_TO_MIXWAL"*2)
        a, b = await asyncio.wait([create_task(readables_to_mixwal_event.wait())], timeout=60)
        if not len(a):
            print('readables_to_mixwal_event.wait() timed out, nothing new to read?')
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
            print("readable_peers", len(readable_peers))
            for (cpeer, rcw) in readable_peers:
                logger.debug("going to process_box", cpeer.name, rcw.next_index[:8].hex())
                try:
                  mw = await process_box(cpeer, rcw)
                except Exception as e:
                  logger.critical(f"process_box failed: {e}")
                  continue
                sess.add(mw)
                print("finished one peer", cpeer.name)
            print("committing")
            await sess.commit()
        print("done readables_to_mixwal", len(readable_peers))
        if len(readable_peers):
            __mixwal_updated.set()
            print("__mixwal_updated.set() from readables_to_mixwal")

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
    while True:
        _, _ = await asyncio.wait((create_task(resendable_event.wait()),), timeout=60)
        resendable_event.clear()
        print("SEND_RESENDABLE RUNNING")
        pwals_to_send = set()
        async with persistent.asession() as sess:
            query = persistent.PlaintextWAL.find_resendable(__resend_queue)
            #print("send_resendable QUERY:", query, __resend_queue)
            sendable = (await sess.exec(query)).all()
            for pwal in sendable:
                if pwal.indirection is None:
                    continue
                # This is an indirection message, we need to fill the read cap.
                # find_resendable should only give us these entries if the needed read cap has been provisioned.
                print("Got a PWAL entry that requires an indirection", pwal.indirection)
                if not pwal.bacap_payload:
                    # TODO this ought to be a SQL UPDATE plaintextwal USING readcapwal, but for now we do it by hand.
                    rcw = sess.get(persistent.ReadCapWAL, pwal.indirection)
                    pwal.bacap_payload = b'I' + rcw.write_cap
                    await sess.update(pwal)
                    await sess.refresh(pwal)
            await sess.commit()
        for pwal in sendable:
            if pwal.bacap_stream not in __resend_queue:
                __resend_queue.add(pwal.bacap_stream)
                t = create_task(start_resending(connection, pwal))
                on_error(t, lambda: __resend_queue.discard(pwal.bacap_stream))  # when cancelled/exception

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

    next_message_index = await connection.next_message_box_index(wc.next_index)

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
        print("ON_CONNECTION_STATUS err:", status)
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
        print("got reply for something we have a queue for", reply['message_id'].hex(), reply['payload'])
        create_task(async_queue.put(reply))
        print("its now on queue", reply['message_id'].hex())
    else:
        print("on_message_reply"*100, reply)
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
        print("ERR for outgoing message_id:", reply['message_id'].hex())
    else:
        print("MESSAGE SENT OK:", reply['message_id'].hex(), reply)

# from katzenpost_thinclient import ThinClient, Config
async def reconnect() -> ThinClient:
    cfg = ThinClientConfig(
        "config/thinclient.toml",
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
    pki = connection.pki_document()
    for sn in (pki or {}).get('ServiceNodes', []):
        snd = cbor2.loads(sn)
        if 'courier' not in snd['Kaetzchen']:
            continue
        snd_dest = hashlib.blake2b(snd['IdentityKey'], digest_size=32).digest()
        if snd_dest == destination:
            return True
    return False

async def test_keypair(connection, write_cap, read_cap):
    """Test that create_new_keypair() results in usable+matching write/read caps."""
    write_msg_id = secrets.token_bytes(16)
    read_msg_id = secrets.token_bytes(16)
    courier = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    logger.debug("courier exists? %s %s", courier, courier_destination_exists(connection, courier))
    write_chan = await connection.resume_write_channel(write_cap=write_cap, message_box_index=write_cap[-104:])
    wcr : WriteChannelReply = await connection.write_channel(write_chan, payload=b'hello')
    await connection.send_channel_query(channel_id=write_chan, payload=wcr.send_message_payload, dest_node=courier, dest_queue=b'courier',message_id=write_msg_id)
    create_task(connection.close_channel(write_chan))
    await asyncio.sleep(20)
    read_chan = await connection.resume_read_channel(read_cap=read_cap, next_message_index=read_cap[-104:])
    rcr : ReadChannelReply = await connection.read_channel(read_chan, message_box_index=read_cap[-104:])
    for i in range(3):
        await connection.send_channel_query(channel_id=read_chan, payload=rcr.send_message_payload, dest_node=courier, dest_queue=b'courier',message_id=read_msg_id)
        await asyncio.sleep(5)
