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
import logging
import uuid

from sqlmodel import select

from . import models, persistent

logger = logging.getLogger("katzen.voucher")

STEP_MINTED = "minted"
STEP_AWAITING = "awaiting"
STEP_INDUCTING = "inducting"
STEP_DONE = "done"

_INDEX_LEN = 104


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
    return wcr.next_message_box_index


async def _read_box(connection, read_cap: bytes, message_box_index: bytes) -> "tuple[bytes, bytes]":
    """Read one box, blocking until it exists, and return (plaintext, next index).

    no_retry_on_box_id_not_found is left False so kpclientd rides out replication
    lag and an unwritten box: the joiner's poll of box 1 is simply this call,
    which returns once the inductor has written the reply.
    """
    rcr = await connection.encrypt_read(read_cap=read_cap, message_box_index=message_box_index)
    resp = await connection.start_resending_encrypted_message(
        read_cap=read_cap, write_cap=None, message_box_index=message_box_index, reply_index=None,
        envelope_descriptor=rcr.envelope_descriptor,
        message_ciphertext=rcr.message_ciphertext,
        envelope_hash=rcr.envelope_hash,
        no_retry_on_box_id_not_found=False,
    )
    return resp.plaintext, rcr.next_message_box_index


async def _conversation_write_cap(sess, conversation_id: int) -> persistent.WriteCapWAL:
    conv = await sess.get(persistent.Conversation, conversation_id)
    wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
    if wcw is None or wcw.write_cap is None:
        raise RuntimeError(
            f"conversation {conversation_id} has no provisioned write cap yet"
        )
    return wcw


def _add_peer(sess, conversation, name: str, read_cap: bytes) -> None:
    rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=read_cap, next_index=read_cap[-_INDEX_LEN:],
    )
    sess.add(rcw)
    sess.add(persistent.ConversationPeer(
        name=name, read_cap_id=rcw.id, active=True, conversation=conversation,
    ))


async def mint_and_publish(connection, conversation_id: int, display_name: str) -> bytes:
    """Joiner: mint a Voucher over this conversation's MessageStream, publish the
    payload to VoucherStream box 0, and return the Voucher to share out of band."""
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
    return mint.voucher


async def await_and_open(connection, conversation_id: int) -> None:
    """Joiner: poll VoucherStream box 1 for the inductor's reply, open it, move
    this conversation's write cap onto the salt-mutated sequence, and add the
    members named in the reply as peers."""
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

    sealed_reply, _ = await _read_box(connection, voucher_read_cap, box1_index)

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
        for please_add in reply_who.please_adds:
            _add_peer(sess, conv, please_add.display_name, please_add.read_cap)
        row = await sess.get(persistent.PendingVoucher, pv_id)
        await sess.delete(row)
        await sess.commit()


async def derive_read_and_induct(connection, conversation_id: int, peer_name: str, voucher: bytes) -> str:
    """Inductor: derive the VoucherStream from the Voucher, read the joiner's
    payload from box 0, seal a reply carrying the group's read caps, write it to
    box 1, and add the joiner (on their salt-mutated read cap) as a peer. Returns
    the joiner's display name."""
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
    )

    who_reply = await _build_who_reply(conversation_id)
    induct = await connection.voucher_induct(
        voucher=voucher, voucher_payload=voucher_payload, who_reply=who_reply.to_cbor(),
    )

    await _publish_box(connection, derived.voucher_write_cap, box1_index, induct.sealed_reply)

    joiner_name = induct.display_name or peer_name
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        _add_peer(sess, conv, joiner_name, induct.mutated_message_read_cap)
        row = await sess.get(persistent.PendingVoucher, pv_id)
        await sess.delete(row)
        await sess.commit()
    return joiner_name


async def _build_who_reply(conversation_id: int) -> models.GroupChatReplyWho:
    """The existing members' read caps the joiner needs to read the group: the
    inductor's own stream plus any already-active peers."""
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        own_rcw = await sess.get(persistent.ReadCapWAL, conv.own_peer.read_cap_id)
        please_adds = [models.GroupChatPleaseAdd(
            display_name=conv.own_peer.name, read_cap=own_rcw.read_cap,
        )]
        for peer in conv.peers:
            if not peer.active or peer.id == conv.own_peer_id:
                continue
            rcw = await sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is not None and rcw.read_cap is not None:
                please_adds.append(models.GroupChatPleaseAdd(
                    display_name=peer.name, read_cap=rcw.read_cap,
                ))
    return models.GroupChatReplyWho(please_adds=please_adds)
