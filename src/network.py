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

    # Loop over PlaintextWAL messages and encrypt them
    f4 = ensure_future(send_resendable_plaintexts(connection))

    # Loop over PlaintextWAL messages and encrypt them
    f5 = ensure_future(readables_to_mixwal(connection))
    try:
        await f5
    except Exception as xx:
        print("f5 exception",xx)
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
            chan_id, _ = await connection.create_read_channel(read_cap=rcw.read_cap, message_box_index=rcw.next_index)
        else:
            wcw = (await sess.exec(select(persistent.WriteCapWAL).where(persistent.WriteCapWAL.id==mw.bacap_stream))).one()
            chan_id, _, _, _ = await connection.create_write_channel(write_cap=wcw.write_cap, message_box_index=wcw.next_index)
        message_id = secrets.token_bytes(16)
        orig_message_id = message_id
        __on_message_queues[message_id] = asyncio.Queue()
        reply = False
        while not reply:
            await connection.send_channel_query(
                channel_id=chan_id,
                payload=mw.encrypted_payload,
                dest_node=mw.destination,
                dest_queue=b'courier',
                message_id=message_id
            )
            reply = await __on_message_queues[message_id].get()
            if reply is None:
                message_id = secrets.token_bytes(16) # do we need a new message_id per resend??
                __on_message_queues[message_id] = __on_message_queues[orig_message_id]
        #await connection.close_channel(chan_id)
        print("RECEIVED REPLY FOR MESSAGE" * 1000, reply)

    while True:
        _, _ = await asyncio.wait((create_task(__mixwal_updated.wait()),), timeout=60)
        __mixwal_updated.clear()
        print("DRAIN_MIXWAL")
        # read new from mixwal
        async with persistent.asession() as sess:
            new_mixwals = (await sess.exec(persistent.MixWAL.get_new(__resend_queue))).all()
            print("NEW MIXWALS", new_mixwals)
            jobs = []
            for mw in new_mixwals:
                print("="*100, "new mixwal mw:", mw)
                __resend_queue.add(mw.bacap_stream)
                jobs.append(tx_single(sess, mw))
            print("GATHERING")
            await asyncio.gather(*jobs)
            await sess.commit()

async def provision_read_caps(connection: ThinClient):
    """Long-running process to tead persistent.WriteCapWAL and populate ReadCapWAL"""
    print("H"*1000)
    import sqlalchemy as sa
    wait = 0
    while True:
        await asyncio.sleep(wait)
        wait = 30
        async with persistent.asession() as sess:
            for (rcw, wcw) in await sess.exec(sa.select(persistent.ReadCapWAL,persistent.WriteCapWAL).where(persistent.ReadCapWAL.read_cap == None).where(persistent.ReadCapWAL.write_cap_id==persistent.WriteCapWAL.id)): #  &
                print("UPDATING:"*100, rcw,wcw, wcw.write_cap, wcw.next_index)
                print("WRITECAP:", len(wcw.write_cap), wcw.write_cap.hex())
                print("NEXTIDX:", len(wcw.next_index), wcw.next_index.hex())
                #chan_id, _write_cap, rcw.read_cap, rcw.next_index = await connection.create_write_channel(write_cap=wcw.write_cap[:1], message_box_index=wcw.next_index)
                tlen = (8 + 32 + 32 + 32  +32)
                if len(wcw.write_cap) == tlen:
                    # write cap is too small for Unmarshal:
                    pub = nacl.public.PrivateKey(wcw.write_cap[:32]).public_key.encode()
                    print("PUB IS", pub)
                try:
                    #chan_id, _write_cap, read_cap, next_index = await connection.create_write_channel(write_cap=wcw.write_cap[:32]+pub+wcw.write_cap[32:], message_box_index=wcw.next_index)
                    chan_id, read_cap, _write_cap, next_index = await connection.create_write_channel(write_cap=wcw.write_cap, message_box_index=wcw.next_index)
                    print("WTF2"*100, len(_write_cap), len(read_cap))
                except Exception as e:
                    print("BAD TODO", e)
                    return
                if chan_id == 0:
                    print("CHAN ID WAS 0")
                    continue
                if _write_cap != wcw.write_cap:
                    print("write_cap mismatch", _write_cap, wcw.write_cap)
                    continue
                print("GOT CREATE_WRITE_CHANNEL ANSWER", read_cap, next_index)
                await connection.close_channel(chan_id)
                print("CHANNEL CLOSED", read_cap)
                if not read_cap:
                    print("ROLLING BACK"*1000, read_cap, pub)
                    continue
                print("GOT CWC"*100, rcw.read_cap, read_cap)
                rcw.read_cap = read_cap
                rcw.next_index = next_index
                print("SESS.ADD")
                sess.add(rcw)
            print("COMMITTING SESSION")
            await sess.commit()
    print("provisioning read caps","|"*1000)

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
        chan_id, _ = await connection.create_read_channel(read_cap=rcw.read_cap, message_box_index=rcw.next_index)
        print("process box has chan_id", chan_id)
        send_message_payload, next_next_index, reply_index = await connection.read_channel(chan_id)
        print("process_box got this from read_channel: reply_index:",reply_index)
        courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
        mw = persistent.MixWAL(
            bacap_stream=rcw.id,
            envelope_hash=secrets.token_bytes(32), # TODO
            destination=courier,
            encrypted_payload=send_message_payload,
            next_message_index=next_next_index,
            is_read=True
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
    async with persistent.asession() as sess:
        query = persistent.PlaintextWAL.find_resendable(__resend_queue)
        print("send_resendable QUERY:", query, __resend_queue)
        sendable = await sess.exec(query)
        sendable = sendable.all()
        print("adding to __resend_queue:", set(pw.id for pw in sendable), "it already has:", __resend_queue)
        __resend_queue |= set(pw.id for pw in sendable)
    await asyncio.gather(*[
        start_resending(connection, plainwal) for plainwal in sendable
    ])

async def start_resending(connection:ThinClient, pwal: persistent.PlaintextWAL):
    if pwal.bacap_stream in __resend_queue:
        print("Why the fuck are we calling start_resending on this?", pwal.bacap_stream)
        import pdb;pdb.set_trace()
        return
    async with persistent.asession() as sess:
        wc: persistent.WriteCapWAL = (await sess.exec(
            persistent.WriteCapWAL.get_by_bacap_uuid(pwal.bacap_stream))).one()[0]

    chan_id, read_cap, write_cap, cur_message_index = await connection.create_write_channel(write_cap=wc.write_cap, message_box_index=wc.next_index)
    print("WTF3"*100, len(write_cap), len(read_cap))
    if wc.write_cap is None: # this is a new one
        async with persistent.asession() as sess:
            wc = await select(WriteCapWAL).where(id=pwal.bacap_stream)
            wc.write_cap = write_cap
            wc.next_index = cur_message_index # TODO need to save this if it's the first?
            sess.update(wc)
            await sess.commit()

    send_message_payload , next_next_index = await connection.write_channel(chan_id, pwal.bacap_payload)
    await connection.close_channel(chan_id)

    courier: bytes = secrets.choice(katzenpost_thinclient.find_services("courier", connection.pki_document())).to_destination()[0]
    mw = persistent.MixWAL(
        bacap_stream = pwal.bacap_stream,
        envelope_hash = secrets.token_bytes(32),
        destination = courier,
        encrypted_payload = send_message_payload,
        next_message_index = next_next_index,
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

    pass

def on_connection_status(status:"Dict[str,Any]"):
    if status["is_connected"]:
      __mixnet_connected.set()
    else:
      __mixnet_connected.clear()
    if status["err"] is not None:
        print("ON_CONNECTION_STATUS err:", status)
        import pdb;pdb.set_trace()
        pass

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
    if connection:
        # TODO try:
        print("WTF7"*100, len(bacap_write_cap), len(read_cap))
        channel_id, read_cap, write_cap, next_message_index = await connection.create_write_channel(
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
