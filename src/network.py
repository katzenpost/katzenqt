import secrets
import katzenpost_thinclient
from katzenpost_thinclient import ThinClient, ThinClientOfflineError
from katzenpost_thinclient import Config as ThinClientConfig
import hashlib
import cbor2
import struct
import nacl.public

# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio
import traceback
from asyncio import ensure_future
from katzen_util import create_task
from pydantic.dataclasses import dataclass
import persistent

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

import secrets

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
    async def tx_single(sess, mw:persistent.MixWAL) -> bool:
        """Return True if we have removed the MixWAL entry."""
        mw_current_idx = struct.unpack('<Q', mw.current_message_index[:8])[0]
        print(f"TX_SINGLE is_read={mw.is_read} idx:{mw_current_idx} ")
        from sqlmodel import select  # TODO mess
        if mw.is_read:
            # TODO instead of n queries for this we ought to just get them in the join
            rcw = (await sess.exec(select(persistent.ReadCapWAL).where(persistent.ReadCapWAL.id==mw.bacap_stream))).one()
            random_reply_index = secrets.choice([0,1])
            print(f"bacap idx {mw_current_idx} mw random reply index: {random_reply_index}")
            chan_id = await connection.resume_read_channel_query(
                read_cap=rcw.read_cap, next_message_index=mw.current_message_index,
                envelope_descriptor=mw.envelope_descriptor,
                envelope_hash=mw.envelope_hash,
                reply_index=random_reply_index,
            )
        else:
            wcw = (await sess.exec(select(persistent.WriteCapWAL).where(persistent.WriteCapWAL.id==mw.bacap_stream))).one()
            chan_id = await connection.resume_write_channel_query(
                write_cap=wcw.write_cap,
                message_box_index=mw.current_message_index,
                envelope_descriptor=mw.envelope_descriptor, envelope_hash=mw.envelope_hash)
            # 20:40:22.483 ERRO katzenpost/client2: resumeWriteChannelQuery: Failed to parse envelope descriptor: cbor: 1999 bytes of extraneous data starting at index 1
        # for each resent message id we should probably keep listening to the old ones
        message_id = secrets.token_bytes(16)
        orig_message_id = message_id
        __on_message_queues[message_id] = asyncio.Queue()
        reply = False
        # we kind of want to use send_channel_query_await_reply(timeout_seconds=None) here, but it's not ideal to do that within the database session.
        def wait_for_ack(message_id: bytes, mw: persistent.MixWAL, acked_event:asyncio.Event):
            connection.ack_queues[message_id] = asyncio.Queue(1)
            async def got_write_ack():
                """
                if we get a reply and the thing we were sending was a Write, this is a write ACK.
                """
                reply = await connection.ack_queues[message_id].get()
                if reply['error_code']:
                    # can't really do much here except keep resending
                    return
                """if there's no error:
                - add to Sentlog so we stop resending,
                - remove from MixWAL,
                - remove from PlaintextWAL?
                - bump send_resendable,
                - bump drain_mixwal
                """
                print("got write ack; kicking off the next send", reply)
                acked_event.set() # stop resending this mixwal entry
                conv_id = await asyncio.shield(persistent.SentLog.mark_sent(mw, __resend_queue))
                resendable_event.set()  # signal send_resendable_plaintexts
                if conv_id:
                    # update the UX:
                    create_task(conversation_update_queue.put((conv_id,True)))
                create_task(connection.close_channel(chan_id))
            async def got_read_ack():
                """this is a read reply, if it's not empty then we can advance state"""
                reply = await connection.ack_queues[message_id].get()
                resendable_event.set()
                if reply['error_code']:
                    print("Tried to read a box, but we got an error", reply)
                    return
                acked_event.set() # stop resending this mixwal entry
                if not reply['payload']:
                    print("Read a box, but it was empty. Keep reading.")
                    return
                import pdb;pdb.set_trace()
                return
            if mw.is_read:
                return create_task(got_read_ack())
            if not mw.is_read:
                return create_task(got_write_ack())
        print("entering while not reply: loop")
        acked_event = asyncio.Event()  # once *one* of our resends are satisfied we can cancel the rest
        while not reply:
            # set up listeners before we send:
            listener = wait_for_ack(message_id, mw, acked_event)
            print(f"is_read:{mw.is_read} chan:{chan_id}")
            # should maybe open a new channel for each of these
            try:
                await connection.send_channel_query(
                    channel_id=chan_id,
                    payload=mw.encrypted_payload,
                    dest_node=mw.destination,
                    dest_queue=b'courier',
                    message_id=message_id
                )
            except ThinClientOfflineError:
                print("thin client is offline, can't drain mixwal. retrying in 5s")
                del connection.ack_queues[message_id]
                await asyncio.sleep(5)
                continue
            try:
                got_reply = create_task( __on_message_queues[message_id].get() )
                got_ack_in_other_thread = create_task(acked_event.wait())
                done, not_done = await asyncio.wait((
                    got_reply, got_ack_in_other_thread, listener,
                ), timeout=25, return_when=asyncio.FIRST_COMPLETED)
                for not_done_job in not_done:
                    not_done_job.cancel()
                if listener in done:
                    print("LISTENER IN DONE!")
                if got_ack_in_other_thread in done:
                    print("got ack in other thread")
                    return False
                if got_reply in done:
                    print("GOT_REPLY", got_reply)
                    reply = got_reply.result()
                    if not reply['payload']:
                        print("no payload for ",reply)
                        reply = None
                else:
                    print("GOT_REPLY timeout")
                    reply = None
            except Exception as e:
                print(e)
                import pdb;pdb.set_trace()
            if reply is None:
                message_id = secrets.token_bytes(16) # new message_id for each resend
                print("RESENDING QUERY with new message_id", message_id.hex())
                __on_message_queues[message_id] = __on_message_queues[orig_message_id]
                if not __on_message_queues[message_id]:
                    print("RESENDQUERY we have no listener __on_message_queues for", message_id.hex(), orig_message_id.hex())
                    import pdb;pdb.set_trace()
                    print("---")
        print("got reply now looking up bacap_stream, expect to see RECEIVED REPLY")
        create_task(connection.close_channel(chan_id))
        rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
        print("RECEIVED REPLY FOR MESSAGE" * 12, reply)
        mw_current_index = struct.unpack('<Q', mw.current_message_index[:8])[0]
        print("bumping read cap next_index from", mw_current_index, struct.unpack('<2Q', rcw.next_index[:8] + mw.next_message_index[:8]  ))
        rcw.next_index = mw.next_message_index
        print('updating rcw')
        sess.add(rcw)  # update ReadCapWAL.next_index in DB to mw.next_message_index
        if reply['payload'].startswith(b"I"):
            print("received indirection", reply)
            import pdb;pdb.set_trace()
            pass
        elif reply['payload'].startswith(b"F"):
            # it's a discrete message
            print("received Final message", reply)
            pass
        elif reply["payload"].startswith(b"C"):
            print("received C message", reply)
            pass
        else:
            print("received message with invalid prefix, going to stop reading this peer", reply)
            cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
            cp.active = False
            sess.add(cp)
            await sess.delete(mw)
            await sess.commit()
            return True
        cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
        # Here we should write to the ReceivedPiece table:
        # 
        sess.add(persistent.ReceivedPiece(
           read_cap=mw.bacap_stream,
           bacap_index=mw.current_message_index[:8],
           chunk_type=reply['payload'][:1],
           chunk=reply['payload'][1:]
        ))
        # If chunk_type==b"F" that means we have something to reassemble that should be put in a ConversationLog.
        # If chunk_type==b"I" we need to start reading that indirect thing.
        # we need to persist to ConversationLog before we commit the next rcw.next_message_index
        print('creating conversationlog')
        import sqlalchemy
        try:
            sess.add(persistent.ConversationLog(
                conversation_id=cp.conversation.id,
                conversation_peer_id=cp.id,
                conversation_order=select(sqlalchemy.func.count())
                .select_from(persistent.ConversationLog)
                .where(persistent.ConversationLog.conversation_id == cp.conversation.id)
                .scalar_subquery(),
                payload=reply['payload'], # TODO 
            ))
        except Exception as e:
            print("adding ConversationLog FAILED", e)
            raise e
        await sess.delete(mw)  # delete MixWAL read command so we can read something else
        print("committing conversationlog update", mw_current_index)
        c_id = cp.conversation.id
        bacap_uuid = mw.bacap_stream
        await sess.commit()
        print('putting on conversation_update_queue')
        create_task(conversation_update_queue.put((c_id,False)))
        print("SIGNALING WE CAN READ MORE readables_to_mixwal_event.set()")
        __resend_queue.discard(bacap_uuid)  # this should be .remove(), but why is it empty?
        readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
        print("made it to the end")
        return True

    draining_right_now : "Set[uuid.UUID]" = set()
    async def with_own_sess(mw):
        async with persistent.asession() as sess:
            done_with_this = await tx_single(sess, mw)
        if done_with_this:
            draining_right_now.remove(mw.bacap_stream)
        # __resend_queue.remove(mw.bacap_stream)
        __mixwal_updated.set()  # if we were blocking something then it can be sent now
    while True:
        _, _ = await asyncio.wait((create_task(__mixwal_updated.wait()),), timeout=60)
        __mixwal_updated.clear()
        print(f"DRAIN_MIXWAL draining_right_now:{draining_right_now} __resend_queue:{__resend_queue}")
        # TODO drain new from mixwal, this should NOT be a long-running session like it currently is
        async with persistent.asession() as sess:
            new_mixwals = (await sess.exec(persistent.MixWAL.get_new(draining_right_now))).all()
        for mw in new_mixwals:
            if not courier_destination_exists(connection, mw.destination):
                print("mw courier is currently not in PKI")
            print("drain_mixwal: NEW MIXWAL:", struct.unpack("<1Q", mw.current_message_index[:8]), mw.is_read, mw.bacap_stream)
            draining_right_now.add(mw.bacap_stream) # this is the uuid PK
            # __resend_queue.add(mw.bacap_stream)
            create_task(with_own_sess(mw))

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
                    print("GOT NEW WRITE CAP", read_cap, write_cap)
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

def on_message_reply(reply):
    """Gets called each time a message reply comes in, whether it's from
    something we wrote or read.
    TODO pretty annoying that it's not async ...
    """
    # Receives something like:
    # {'message_id': b'\n\x90\xc2\x0cr\xa8\xa2+\x17Y\xcb\x837\xcc\x0f\x9b', 'surbid': None, 'payload': None}
    if async_queue := __on_message_queues.get(reply['message_id'], None):
        print("got reply for something we have a queue for", reply['message_id'].hex(), reply['payload'])
        async_queue.put_nowait(reply)
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

def on_message_sent(reply):
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
    """
    pki = connection.pki_document()
    import cbor2
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
    import secrets
    write_msg_id = secrets.token_bytes(16)
    read_msg_id = secrets.token_bytes(16)
    courier = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    print("courier:",courier)
    does_destination_exist(connection)
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
