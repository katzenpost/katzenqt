import secrets
import katzenpost_thinclient
from katzenpost_thinclient import ThinClient
from katzenpost_thinclient import Config as ThinClientConfig
import cbor2
import nacl.public

# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio
from asyncio import ensure_future, create_task
from pydantic.dataclasses import dataclass
import persistent

__resend_queue: "Set[uuid.UUID]" = set()
#__plaintextwal_updated = asyncio.Event()
#__plaintextwal_updated.set()
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

async def populate_resend_queue():
    global __resend_queue
    __resend_queue |= await persistent.MixWAL.resend_queue_from_disk()

async def start_background_threads(connection: ThinClient):
    """This should be called on startup, after establishing a connection to the mixnet."""
    # Loop over our write caps and retrive read caps for them:
    f1 = ensure_future(provision_read_caps(connection))

    await __mixnet_connected.wait()
    f2 = readables_to_mixwal(connection)
    await f2

    # Loop over outgoing queue and start transmitting them to the network.
    # This needs to happen before we call send_resendable to avoid:
    f3 = ensure_future(drain_mixwal(connection))
    await f3
    # TODO then we don't need       populate_resend_queue(),

    # Loop over old encrypted messages and resend them
    f4 = ensure_future(send_resendable_plaintexts(connection))

    # Loop over things we can read and start reading them:
    f5 = ensure_future(readables_to_mixwal(connection))
    try:
        await asyncio.gather(f1, f4, f5)
    except Exception as xx:
        print("f1-f4-f5 exception",xx)
    async def do_shutdown():
        """TODO this needs some work"""
        await __should_quit.wait()
        f1.cancel()
        f2.cancel()
        f3.cancel()
        f4.cancel()
        f5.cancel()
        f6.cancel()
    shutdown_task = asyncio.create_task(do_shutdown())


async def drain_mixwal(connection: ThinClient):
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
    async def tx_single(sess, mw:persistent.MixWAL):
        print("TX_SINGLE"*100, mw.is_read)
        from sqlmodel import select  # TODO mess
        if mw.is_read:
            # TODO instead of n queries for this we ought to just get them in the join
            rcw = (await sess.exec(select(persistent.ReadCapWAL).where(persistent.ReadCapWAL.id==mw.bacap_stream))).one()
            chan_id = await connection.resume_read_channel_query(
                read_cap=rcw.read_cap, next_message_index=rcw.next_index,
                envelope_descriptor=mw.envelope_descriptor, #ncrypted_payload,
                envelope_hash=mw.envelope_hash,
                reply_index=0,
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
        def wait_for_ack(message_id: bytes, mw: persistent.MixWAL):
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
                await persistent.SentLog.mark_sent(mw, __resend_queue)
                resendable_event.set()  # signal send_resendable_plaintexts
            async def got_read_ack():
                """this is a read reply, if it's not empty then we can advance state"""
                reply = await connection.ack_queues[message_id].get()
                if reply['error_code']:
                    print("Tried to read a box, but we got an error", reply)
                    return
                if not reply['payload']:
                    print("Read a box, but it was empty. Keep reading.")
                    return
                import pdb;pdb.set_trace()
                return
            if mw.is_read:
                asyncio.create_task(got_read_ack())
            if not mw.is_read:
                asyncio.create_task(got_write_ack())
        while not reply:
            wait_for_ack(message_id, mw)
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
            reply = await __on_message_queues[message_id].get()
            if reply is None:
                print("RESENDING QUERY")
                message_id = secrets.token_bytes(16) # do we need a new message_id per resend??
                __on_message_queues[message_id] = __on_message_queues[orig_message_id]
        await connection.close_channel(chan_id)
        # TODO may need to advance next_index in DB
        print("RECEIVED REPLY FOR MESSAGE" * 1000, reply)

    draining_right_now : "Set[uuid.UUID]" = set()
    async def with_own_sess(mw):
        async with persistent.asession() as sess:
            await tx_single(sess, mw)
        draining_right_now.remove(mw.id)
    while True:
        _, _ = await asyncio.wait((create_task(__mixwal_updated.wait()),), timeout=60)
        __mixwal_updated.clear()
        print("DRAIN_MIXWAL", draining_right_now)
        # TODO drain new from mixwal, this should NOT be a long-running session like it currently is
        async with persistent.asession() as sess:
            new_mixwals = (await sess.exec(persistent.MixWAL.get_new(draining_right_now))).all()
        print("drain_mixwal: NEW MIXWALS", new_mixwals)    
        jobs = []
        for mw in new_mixwals:
            draining_right_now.add(mw.id) # this is the uuid PK
            print("="*100, "new mixwal mw:", mw)
            #__resend_queue.add(mw.bacap_stream)  # TODO unsure if this does is still used?
            asyncio.create_task(with_own_sess(mw))

async def provision_read_caps(connection: ThinClient):
    """Long-running process to tread persistent.WriteCapWAL and populate ReadCapWAL"""
    #print("provision read caps"*100)
    import sqlalchemy as sa
    wait = 0
    while True:
        await asyncio.sleep(wait)
        wait = 5
        async with persistent.asession() as sess:
            for (rcw, wcw) in await sess.exec(sa.select(persistent.ReadCapWAL,persistent.WriteCapWAL).where(persistent.ReadCapWAL.read_cap == None).where(persistent.ReadCapWAL.write_cap_id==persistent.WriteCapWAL.id)): #  &
                print("UPDATING:"*100, rcw,wcw, wcw.write_cap, wcw.next_index)
                if wcw.write_cap is None:
                    try:
                        chan_id, read_cap, write_cap = await connection.create_write_channel()
                        await connection.close_channel(chan_id)
                    except Exception as e:
                        print("create_write_channel DID NOT WORK"*100)
                        print(e)
                        continue
                    print("GOT NEW WRITE CAP", read_cap, write_cap)
                    wcw.write_cap = write_cap
                    wcw.next_index = write_cap[-104:]
                    rcw.read_cap = read_cap
                    rcw.next_index = read_cap[-104:]
                    #assert wcw.next_index == rcw.next_index
                    sess.add(wcw)
                    sess.add(rcw)
                    await sess.commit()
                    await sess.refresh(wcw)
                    await sess.refresh(rcw)
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
    async def process_box(sess, cpeer:persistent.ConversationPeer, rcw:persistent.ReadCapWAL) -> None:
        print("process box cpeer-rcw:", cpeer, rcw)
        chan_id = await connection.resume_read_channel(read_cap=rcw.read_cap,next_message_index=rcw.next_index) #, message_box_index=rcw.next_index)
        print("process box has chan_id", chan_id)
        #send_message_payload, next_next_index, reply_index
        rcreply : "katzenpost_thinclient.ReadChannelReply" = await connection.read_channel(chan_id, message_box_index=rcw.next_index)
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
        sess.add(mw)
    async with persistent.asession() as sess:
        readable_peers = (await sess.exec(select(
            persistent.ConversationPeer, persistent.ReadCapWAL
        ).where(persistent.ConversationPeer.active==True
                ).where(persistent.ConversationPeer.read_cap_id == persistent.ReadCapWAL.id
                        ).where(
                            persistent.ReadCapWAL.id.not_in(select(persistent.MixWAL.bacap_stream))
                        ))).all()
        print("READABLE"*100, readable_peers)
        await asyncio.gather(*[process_box(sess, *cprw) for cprw in readable_peers])
        await sess.commit()
    if len(readable_peers):
        __mixwal_updated.set()

async def send_resendable_plaintexts(connection:ThinClient) -> None:
    # TODO this needs to be protected by a mutex or use an asyncio.Event
    # look at persistent.PlaintextWAL:
    # PICK OUT stuff in plaintextwall that we aren't currently resending
    # - mark them as "being resent" (in memory)
    # - when we get an ACK, we move it from being resent to "sent" (db),
    #   and remove it from the PlaintextWAL, atomically
    global __resend_queue
    while True:
        print("WAITING FOR RESENDABLE")
        _, _ = await asyncio.wait((create_task(resendable_event.wait()),), timeout=60)
        resendable_event.clear()
        print("SEND_RESENDABLE RUNNING")
        pwals_to_send = set()
        async with persistent.asession() as sess:
            query = persistent.PlaintextWAL.find_resendable(__resend_queue)
            print("send_resendable QUERY:", query, __resend_queue)
            sendable = (await sess.exec(query)).all()
        for pwal in sendable:
            if pwal.bacap_stream not in __resend_queue:
                asyncio.create_task(start_resending(connection, pwal))

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
        wc: persistent.WriteCapWAL = (await sess.exec(
            persistent.WriteCapWAL.get_by_bacap_uuid(pwal.bacap_stream))).one()[0]

    if wc.write_cap is None: # this is a new one
        print("WHY IS WRITE CAP EMPTY HERE")
        import pdb;pdb.set_trace()
        chan_id, read_cap, write_cap = await connection.create_write_channel()
        await connection.close_channel(chan_id)
        async with persistent.asession() as sess:
            wc = await select(WriteCapWAL).where(id=pwal.bacap_stream, write_cap=None)
            wc.write_cap = write_cap
            wc.next_index = write_cap[-104:] # TODO why doesn't create_write_channel() give us this?
            sess.add(wc)
            # we probably want to populate ReadCapWAL too while we are at this
            await sess.commit()
            await sess.refresh(wc)
        # now we have a write_cap and a next_index

    # now we have:
    # - a plaintext to send to send, pwal.bacap_payload
    # - wc has .write_cap, .next_index
    # and we need to:
    # - pick a courier
    # - encrypt the message
    # - persist that to MixWAL

    chan_id = await connection.resume_write_channel(write_cap=wc.write_cap, message_box_index=wc.next_index)
    print("-"*1000, "start_resending made a resume channel")
    wcr = await connection.write_channel(chan_id, payload=pwal.bacap_payload)
    print("-"*1000, "start_resending wrote to channel", wcr)
    await connection.close_channel(chan_id)
    print("-"*1000, "start_resending closed channel")
    
    courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    mw = persistent.MixWAL(
        bacap_stream = pwal.bacap_stream,
        plaintextwal=pwal.id,
        envelope_hash = wcr.envelope_hash,
        destination = courier,
        encrypted_payload = wcr.send_message_payload,
        envelope_descriptor = wcr.envelope_descriptor,
        current_message_index = wcr.current_message_index,
        next_message_index = wcr.next_message_index, # for resends, do we need current_message_index ? 
        is_read = False
    )
    print("-"*1000, "start_resending made a MixWAL entry")
    async with persistent.asession() as sess:
        sess.add(mw)
        # TODO if we do this, how do we know what to remove from PlaintextWAL?
        # we need to hook network.on_message_reply to look for the returns
        await sess.commit()

    print("-"*1000, "start_resending actually did something")
    # Now we have persisted our intention to resend.
    # Next up is something needs to actually resend, reading from MixWAL
    # and issuing ThinClient.send_channel_query
    __mixwal_updated.set()
    pass

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
        async_queue.put_nowait(reply['payload'])
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
    'reply_eta': 0,
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
    # TODO can .start fail?
    await client.start(asyncio.get_running_loop())
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
