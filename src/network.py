import secrets

import katzenpost_thinclient

# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio

# from katzenpost_thinclient import ThinClient, Config
async def reconnect() -> None:
    return

# events: we should keep track of ConnectionStatusEvent.IsConnected so we can tell the user whether the mixnet client is working

# ThinClient.send_message(surb_id, payload, dest_node, dest_queue)

async def wal_sender():
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
    pass


async def new_write_cap(connection=None) -> bytes:
    """NewWriteChannel{}

    TODO: how does client2 handle if we ask to recreate one? do we get the same handle, or an error, or? there should be a test for that if there isn't.
    """
    # ThinClient.new_write_channel()
    # listen for the reply
    # ThinClient.delete_write_channel()  # this doesn't exist yet
    # return the reply's bacap_write_cap
    return b"write:" + secrets.token_hex()[:8].encode()

async def read_cap_from_write_cap(write_cap: bytes) -> bytes:
    return b"read:" + write_cap.replace(b"write:", b"")

async def encrypt_message(msg: str, bacap_write_cap:bytes, bacap_write_index:int) -> bytes:
    """cbor encode a chat message and encrypt it using write_cap

    ThinClient.WriteChannelMessage() -> should give us a payload that we can put in ThinClient.SendMessage
    """
    print("encrypt_message:", msg, bacap_write_cap, bacap_write_index)
    return b"encrypted:{msg.decode()}:{bacap_write_cap}:{bacap_write_index}"
