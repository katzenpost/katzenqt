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
from katzen_util import create_task
from pydantic.dataclasses import dataclass
import persistent

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
    f1 = ensure_future(provision_read_caps(connection))

    await __mixnet_connected.wait()

    # Loop over outgoing queue and start transmitting them to the network. Runs forever.
    f3 = ensure_future(drain_mixwal(connection))

    # Loop over PlaintextWAL messages, encrypt them, and put them in MixWAL. Runs forever.
    f4 = ensure_future(send_resendable_plaintexts(connection))

    # Loop over things we can read and start reading them:
    f5 = create_task(readables_to_mixwal(connection))
    async def do_shutdown():
        """TODO this needs some work"""
        await __should_quit.wait()
        print("shutting down")
        f1.cancel()
        f3.cancel()
        f4.cancel()
        f5.cancel()
    shutdown_task = create_task(do_shutdown())
    try:
        for task in asyncio.as_completed([f1, f3, f4, f5, shutdown_task]):
            await task
            print("start_background_threads completed: ",task)
    except Exception as xx:
        print("f1-f4-f5 exception",xx)
        raise

async def drain_mixwal(connection: ThinClient):
    try:
        await drain_mixwal2(connection) # todo why the fuck does this not catch ?
    except Exception as e:
        print("drain_mixwal: exception", e)
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
    chan_id = await connection.resume_write_channel_query(
        write_cap=wcw.write_cap,
        message_box_index=mw.current_message_index,
        envelope_descriptor=mw.envelope_descriptor, envelope_hash=mw.envelope_hash)
    ack_queue = asyncio.Queue()  # unbounded since we reuse it
    async def receive_acks() -> None:
        while True:
            ack = await ack_queue.get()
            if ack['error_code'] != 0:
                logger.error(f"got ack with error_code={ack['error_code']}")
                continue
            return
    
    async def send_message() -> None:
        """resend a single message to courier"""
        message_id = secrets.token_bytes(16)
        connection.ack_queues[message_id] = ack_queue
        try:
            await connection.send_channel_query(
                channel_id=chan_id,
                payload=mw.encrypted_payload,
                dest_node=mw.destination,
                dest_queue=b'courier',
                message_id=message_id,
            )
        except ThinClientOfflineError:
            logger.warning("thin client is offline, can't drain mixwal.")
            del connection.ack_queues[message_id]
            await asyncio.sleep(5)

    final_task = create_task(receive_acks())
    resend_tasks = set()
    done = set()

    while final_task not in done:
        send_message_task = create_task(send_message())
        resend_tasks.add(send_message_task)
        done, _ = await asyncio.wait([final_task], timeout=5 + random.random()*10)

    for unneeded in resend_tasks:
        unneeded.cancel()

    create_task(connection.close_channel(chan_id))

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
    - Wait for a response
    - If we don't get response: resend after N seconds.
      - Each resend needs a new message id.
    - If we get a response:
      - A message: We can progress
      - A box not found:
        - We should: Resend at at later time
        - Do to a bug in the courier/replica: We actually need to:
          - Wait a bit
          - Remove the MixWAL entry to have a new envelope be made
    """
    assert mw.is_read
    bacap_uuid = mw.bacap_stream

    # we should check that mw.destination exists:
    if not courier_destination_exists(connection, mw.destination):
        logger.error("outbound read mw for courier that no longer exists")
        return

    # we should actually have two of these, one for each reply_index

    # we need a read_cap=rcw.read_cap

    logger.debug("reading box")
    reply_queue = asyncio.Queue()
    reply_task = create_task(reply_queue.get())
    chan_tasks = []
    chans = []
    for reply_index in [0,1]:
        chan_task = create_task(connection.resume_read_channel_query(
            read_cap=rcw_read_cap, next_message_index=mw.current_message_index,
            envelope_descriptor=mw.envelope_descriptor,
            envelope_hash=mw.envelope_hash,
            reply_index=reply_index,
        ))
        chan_task.add_done_callback(lambda t: chans.append(t.result()))
        chan_tasks.append(chan_task)
    await asyncio.wait(chan_tasks)
    logger.critical(f"chans: {chans}")
    attempts = 8
    message_ids = []
    reply_tasks = []
    ack_tasks = []
    not_found = {chan: asyncio.Event() for chan in chans}
    async def never_found():
        for chan in chans:
            await not_found[chan].wait()
    async def retry_fetch(chan_id: int):
        for i in range(attempts):
            if not_found[chan_id].is_set():
                logger.warning(f"not_found for {chan_id} so skipping resend of read")
                await asyncio.sleep(10)
                continue
            message_id = secrets.token_bytes(16)
            # TODO: clean up these queues
            __on_message_queues[message_id] = reply_queue  # populated by global on_message_reply
            connection.ack_queues[message_id] = asyncio.Queue(1)
            async def handle_ack(chan_id):
                """TODO: we could stop sending to this chan_id if we get a 'BoxNotFound' since that will never succeed."""
                ack = await connection.ack_queues[message_id].get()
                if ack['error_code'] != 0:
                    logger.critical(f"channel {chan_id} ack: {ack}")
                    if ack['error_code'] == 22:  # box not found; at the moment that means further reads will never work.
                        not_found[chan_id].set()
                else:
                    logger.critical(f"DEBUG READ: got {ack}")
            ack_task = create_task(handle_ack(chan_id))
            ack_tasks.append(ack_task)
            # IFF we get an ack, and the error code in the ACK says "box not found" then we need to abort reading from this
            # reply index.
            message_ids.append(message_id)
            # The first one we send will never return anything useful because the courier immediately responds:
            await connection.send_channel_query(
                channel_id=chan_id,
                payload=mw.encrypted_payload,
                dest_node=mw.destination,
                dest_queue=b'courier',
                message_id=message_id
            )
            if i == 0:  # First message gets a bit longer
                await asyncio.sleep(3+random.random())
            await asyncio.sleep(4+secrets.randbelow(5)+random.random())

    # wait for either a message or an empty ACK,
    # or a timeout (in case the message got dropped)
    # OR: another thread finished handling this

    done, not_done = await asyncio.wait(
        [reply_task, create_task(never_found())] + [create_task(retry_fetch(chan_id)) for chan_id in chans],
        timeout=10*attempts,
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in not_done:
        task.cancel()
    for ack_task in ack_tasks:
        ack_task.cancel()
    for chan_id in chans:
        create_task(connection.close_channel(chan_id))

    # So now we either have:
    # both
    # neither
    # ack
    # reply

    #import pdb;pdb.set_trace()
    # A tricky situation arises if we receive both an ACK and a message payload.
    if reply_task in done:
        reply = reply_task.result()
        logger.critical("got message reply!", reply)
        if reply is None:
            import pdb;pdb.set_trace()
        # look up rcw
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

            if reply['payload'].startswith(b"I"):
                logger.debug(f"received indirection {reply}")
                import pdb;pdb.set_trace()
                pass
            elif reply['payload'].startswith(b"F"):
                # it's a discrete message
                logger.debug(f"received Final message {reply}")
                pass
            elif reply["payload"].startswith(b"C"):
                logger.debug(f"received C message {reply}")
                pass
            else:
                print("received message with invalid prefix, going to stop reading this peer", reply)
                cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
                cp.active = False
                sess.add(cp)
                await sess.delete(mw)
                await sess.commit()
                __mixwal_updated.set()
                return

            from sqlmodel import select
            cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
            # Here we should write to the ReceivedPiece table:
            #
            sess.add(persistent.ReceivedPiece(
                read_cap=mw.bacap_stream,
                bacap_index=mw.current_message_index[:8],
                chunk_type=reply['payload'][:1],
                chunk=reply['payload'][1:]
            ))
            cl = persistent.ConversationLog.append_from(cp, reply["payload"])
            sess.add(cl)
            await sess.delete(mw)
            c_id = cp.conversation.id
            bacap_uuid = mw.bacap_stream
            await sess.commit()
        create_task(conversation_update_queue.put((c_id,False)))
        done = []  # disregard the ack_task
    else:
        logger.critical("NO MESSAGE REPLY, making new envelope"*100)
        async with persistent.asession() as sess:
            await sess.delete(mw)
            await sess.commit()

    """
    if ack_task in done:
        ack = ack_task.result()
        logger.critical(f"got ack! {ack}")
        if ack['error_code'] != 0:
            import pdb; pdb.set_trace()
            pass
        # assume error_code == 0:
        # this is what we get when the box is empty. normally we would just return here.
        # unfortunately reading nonexistent messages currently gets cached by the courier,
        # so instead we force a new envelope creation by deleting the mixwal entry:
        async with persistent.asession() as sess:
            await sess.delete(mw)
            await sess.commit()
    """

    # TODO clean up connection.ack_queues[message_id] and __on_message_queues
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
    while True:
        _, _ = await asyncio.wait((create_task(__mixwal_updated.wait()),), timeout=15)
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
    while True:
        await asyncio.sleep(wait)  # could make this smoother with an asyncio.Event(), but 5s is fine for now.
        wait = 5
        async with persistent.asession() as sess:
            for (rcw, wcw) in await sess.exec(sa.select(persistent.ReadCapWAL,persistent.WriteCapWAL).where(persistent.ReadCapWAL.read_cap == None).where(persistent.ReadCapWAL.write_cap_id==persistent.WriteCapWAL.id)): #  &
                print("UPDATING:"*100, rcw,wcw, wcw.write_cap, wcw.next_index)
                if wcw.write_cap is None:
                    try:
                        chan_id, read_cap, write_cap = await connection.create_write_channel()
                        create_task(connection.close_channel(chan_id))
                    except Exception as e:
                        print("create_write_channel DID NOT WORK"*100)
                        print(e)
                        continue
                    wcw.write_cap = write_cap
                    wcw.next_index = write_cap[-104:]
                    rcw.read_cap = read_cap
                    rcw.next_index = read_cap[-104:]
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
    from sqlmodel import select  # TODO move to persistent
    global __resend_queue
    await __resend_queue_populated.wait()
    async def process_box(cpeer:persistent.ConversationPeer, rcw:persistent.ReadCapWAL) -> persistent.MixWAL:
        print("process box cpeer-rcw:", cpeer, struct.unpack('<Q', rcw.next_index[:8]))
        try:
            chan_id = await connection.resume_read_channel(read_cap=rcw.read_cap,next_message_index=rcw.next_index)
            print(f"cpeer got chan_id:{chan_id}, waiting for read_channel")
            rcreply : "katzenpost_thinclient.ReadChannelReply" = await connection.read_channel(chan_id, message_box_index=rcw.next_index)
            print(f"cpeer got read_channel reply")
        except Exception as e:
            print("readables-to-mixwal", e)
            import pdb;pdb.set_trace()
        print("process_box got this from read_channel:", rcreply)
        courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
        mw = persistent.MixWAL(
            bacap_stream=rcw.id,
            plaintextwal=None,
            envelope_hash=rcreply.envelope_hash,
            destination=courier,
            encrypted_payload=rcreply.send_message_payload,
            envelope_descriptor=rcreply.envelope_descriptor,
            next_message_index=rcreply.next_message_index,
            current_message_index=rcreply.current_message_index,
            # rcreply.reply_index
            is_read=True,
        )
        return mw
    while True:
        print("SLEEPING FOR READABLES_TO_MIXWAL"*2)
        a, b = await asyncio.wait([create_task(readables_to_mixwal_event.wait())], timeout=60)
        if not len(a):
            print('readables_to_mixwal_event.wait() timed out, nothing new to read?')
            continue
        readables_to_mixwal_event.clear()
        print("IN READABLES_TO_MIXWAL_LOOP")
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
                print("going to process_box", cpeer.name, rcw.next_index[:8].hex())
                sess.add(await process_box(cpeer, rcw)) ## HERE?
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

    chan_id = await connection.resume_write_channel(write_cap=wc.write_cap, message_box_index=wc.next_index)
    wcr = await connection.write_channel(chan_id, payload=pwal.bacap_payload)
    create_task(connection.close_channel(chan_id))
    
    courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    mw = persistent.MixWAL(
        bacap_stream=pwal.bacap_stream,
        plaintextwal=pwal.id,
        envelope_hash = wcr.envelope_hash,
        destination = courier,
        encrypted_payload = wcr.send_message_payload,
        envelope_descriptor = wcr.envelope_descriptor,
        current_message_index = wcr.current_message_index,
        next_message_index = wcr.next_message_index, # for resends, do we need current_message_index ? 
        is_read = False
    )
    async with persistent.asession() as sess:
        sess.add(mw)
        # TODO if we do this, how do we know what to remove from PlaintextWAL?
        # we need to hook network.on_message_reply to look for the returns
        await sess.commit()
    # Now we have persisted our intention to resend.
    # Next up is something needs to actually resend, reading from MixWAL
    # and issuing ThinClient.send_channel_query
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
        "thinclient.toml",
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


@dataclass
class EncryptMessageReply:
    encrypted: bytes
    next_write_index: bytes

async def encrypt_message(msg: str, bacap_write_cap:bytes, bacap_write_index:int, connection:ThinClient|None=None) -> EncryptMessageReply:
    """cbor encode a chat message and encrypt it using write_cap

    ThinClient.WriteChannelMessage() -> should give us a payload that we can put in ThinClient.SendMessage
    """
    payload = cbor2.dumps({
        'version': 0,
        'text': msg
    })
    print("ENCRYPT_MESSAGE DOES NOT WORK")
    return
    if connection:
        # TODO try:
        print("WTF7"*100, len(bacap_write_cap), len(read_cap))
        channel_id, read_cap, write_cap = await connection.create_write_channel(
            write_cap=bacap_write_cap,
            message_box_index=bacap_write_index,
        )
        print("WTF6"*100, len(write_cap), len(read_cap))
        encrypted, next_message_index = await connection.write_channel(
            channel_id=channel_id,
            payload=payload
        )
        await connection.close_channel(channel_id)
        return EncryptMessageReply(encrypted=encrypted, next_write_index=next_message_index)
    print("encrypt_message:", msg, bacap_write_cap, bacap_write_index)
    return EncryptMessageReply(
        encrypted=b"encrypted:{payload}:{bacap_write_cap}:{bacap_write_index}",
        next_write_index=b"1234"
    )

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
    for sn in pki['ServiceNodes']:
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
