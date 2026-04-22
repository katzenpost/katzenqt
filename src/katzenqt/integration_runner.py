"""Headless CLI runner used by the Alice/Bob integration tests.

Each subprocess invocation performs one step against a KQT_STATE-scoped
SQLite instance and prints one machine-readable line to stdout. See
`tests/integration/test_alice_bob.py` for the driving harness.

Actions:

    create-conv CONV_NAME OWN_DISPLAY_NAME
        Create a new Conversation + own ConversationPeer, wait for the
        background provision_read_caps loop to fill in the WriteCap/ReadCap
        via `ThinClient.new_keypair`, then print `INVITE=<base64>`.

    accept-invite CONV_NAME OWN_DISPLAY_NAME PEER_NAME INVITE_B64
        Create a Conversation + own ConversationPeer, then insert a
        ReadCapWAL from the invite and a ConversationPeer marked
        `active=True` so the read loop picks it up. Prints `ACCEPTED`.

    send CONV_NAME TEXT
        Insert a PlaintextWAL via SendOperation; wait until the network
        reports the MixWAL drained (SentLog entry appears). Prints `SENT`.

    read CONV_NAME TIMEOUT_S
        Poll the ConversationLog until a peer message arrives (anything
        whose ConversationPeer is not the own_peer) or the timeout fires.
        Prints `RECV=<text>` or `TIMEOUT`.

The runner connects to kpclientd using the same thinclient config katzenqt
uses (`config/thinclient.toml`), unless KATZENQT_THINCLIENT_CONFIG overrides
it.
"""
import argparse
import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlmodel import SQLModel, select

from . import models, network, persistent


_DEFAULT_THINCLIENT_CONFIG = str(
    Path(__file__).resolve().parent.parent.parent / "config" / "thinclient.toml"
)


def _cfg_path() -> str:
    return os.environ.get("KATZENQT_THINCLIENT_CONFIG", _DEFAULT_THINCLIENT_CONFIG)


async def _connect_and_start():
    """Connect to kpclientd and kick the background threads running.

    Returns a tuple of (connection, background_task). The caller is
    responsible for cancelling the background task on exit.
    """
    # network.reconnect() reads the config path from the hardcoded
    # "config/thinclient.toml". Temporarily chdir so its relative lookup
    # resolves to the config we want.
    os.chdir(str(Path(_cfg_path()).resolve().parent.parent))
    connection = await network.reconnect()
    bg = asyncio.create_task(network.start_background_threads(connection))
    return connection, bg


async def _shutdown(bg):
    network.shutdown()
    try:
        await asyncio.wait_for(bg, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        bg.cancel()


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

async def _action_create_conv(args):
    # Build the conversation + own_peer skeleton; provision_read_caps will
    # fill in the WriteCap/ReadCap asynchronously once the daemon is up.
    wcapwal = persistent.WriteCapWAL(id=uuid.uuid4())
    rcapwal = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcapwal.id)
    convo = persistent.Conversation(name=args.conv_name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name=args.own_display, read_cap_id=rcapwal.id,
        active=False, conversation=convo,
    )
    convo.own_peer = own_peer
    first_post = persistent.ConversationLog(
        conversation=convo, conversation_peer=own_peer,
        conversation_order=0,
        payload=b"You created this conversation",
    )

    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(first_post)
        await sess.commit()
        await sess.refresh(rcapwal)

    connection, bg = await _connect_and_start()
    try:
        # Wait up to 60s for provision_read_caps to populate the ReadCap.
        for _ in range(120):
            async with persistent.asession() as sess:
                r = await sess.get(persistent.ReadCapWAL, rcapwal.id)
                if r is not None and r.read_cap is not None:
                    rc_bytes = r.read_cap
                    break
            await asyncio.sleep(0.5)
        else:
            print("ERROR=no read_cap provisioned after timeout", flush=True)
            return 2

        invite = models.GroupChatPleaseAdd(
            display_name=args.own_display, read_cap=rc_bytes,
        )
        print("INVITE=" + invite.to_human_readable(), flush=True)
        return 0
    finally:
        await _shutdown(bg)


async def _action_accept_invite(args):
    please_add = models.GroupChatPleaseAdd.from_human_readable(args.invite_b64)

    # Build own conversation + own_peer skeleton.
    wcapwal = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcapwal = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcapwal.id)
    convo = persistent.Conversation(name=args.conv_name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name=args.own_display, read_cap_id=own_rcapwal.id,
        active=False, conversation=convo,
    )
    convo.own_peer = own_peer

    # ReadCapWAL + ConversationPeer for the inviter.
    peer_rcwal = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        read_cap=please_add.read_cap,
        next_index=please_add.read_cap[-104:],
    )
    peer = persistent.ConversationPeer(
        name=args.peer_name, read_cap_id=peer_rcwal.id, active=True,
        conversation=convo,
    )

    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(own_rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(peer_rcwal)
        sess.add(peer)
        await sess.commit()

    print("ACCEPTED", flush=True)
    return 0


async def _action_send(args):
    # Locate our Conversation by name.
    async with persistent.asession() as sess:
        convo = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == args.conv_name
            )
        )).first()
        if convo is None:
            print(f"ERROR=conversation {args.conv_name!r} not found", flush=True)
            return 2
        conversation_id = convo.id
        # Outgoing writes for this conversation go on the write-cap's
        # bacap_stream (i.e., the WriteCapWAL primary key). find_resendable
        # requires the PWAL's bacap_stream to match a fully-provisioned
        # WriteCapWAL, so using e.g. own_peer.read_cap_id silently stalls.
        own_bacap_stream = convo.write_cap

    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8, text=args.text,
    )
    send_op = models.SendOperation(
        bacap_stream=own_bacap_stream, messages=[gcm],
    )
    new_write_caps, db_entries = send_op.serialize(
        chunk_size=1530, conversation_id=conversation_id,
    )
    final_pwal_id = db_entries[-1].id

    async with persistent.asession() as sess:
        for cap_uuid in new_write_caps:
            sess.add(persistent.WriteCapWAL(id=cap_uuid))
        for obj in db_entries:
            sess.add(obj)
        await sess.commit()

    connection, bg = await _connect_and_start()
    try:
        await network.check_for_new()
        # Wait until SentLog contains the final pwal id, meaning mark_sent ran.
        for _ in range(480):  # 120s @ 250ms
            async with persistent.asession() as sess:
                hit = (await sess.exec(
                    select(persistent.SentLog).where(persistent.SentLog.id == final_pwal_id)
                )).first()
                if hit is not None:
                    print("SENT", flush=True)
                    return 0
            await asyncio.sleep(0.25)
        print("ERROR=send timed out waiting for SentLog", flush=True)
        return 3
    finally:
        await _shutdown(bg)


async def _action_read(args):
    async with persistent.asession() as sess:
        convo = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == args.conv_name
            )
        )).first()
        if convo is None:
            print(f"ERROR=conversation {args.conv_name!r} not found", flush=True)
            return 2
        own_peer_id = convo.own_peer_id
        conversation_id = convo.id

    connection, bg = await _connect_and_start()
    try:
        await network.signal_readables_to_mixwal()
        deadline = asyncio.get_event_loop().time() + args.timeout_s
        while asyncio.get_event_loop().time() < deadline:
            async with persistent.asession() as sess:
                rows = (await sess.exec(
                    select(persistent.ConversationLog).where(
                        persistent.ConversationLog.conversation_id == conversation_id
                    ).where(
                        persistent.ConversationLog.conversation_peer_id != own_peer_id
                    )
                )).all()
                for cl in rows:
                    # Peer messages are stored as "<prefix><cbor>" where prefix
                    # is b'F' for final and b'C' for continuation. We only look
                    # at 'F' messages for the single-message test.
                    if cl.payload[:1] != b"F":
                        continue
                    try:
                        gcm = models.GroupChatMessage.from_cbor(cl.payload[1:])
                    except Exception:
                        continue
                    if gcm.text:
                        print("RECV=" + gcm.text, flush=True)
                        return 0
            await asyncio.sleep(0.5)
        print("TIMEOUT", flush=True)
        return 1
    finally:
        await _shutdown(bg)


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="katzenqt.integration_runner",
        description=__doc__.splitlines()[0],
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_create = sub.add_parser("create-conv")
    p_create.add_argument("conv_name")
    p_create.add_argument("own_display")
    p_create.set_defaults(func=_action_create_conv)

    p_accept = sub.add_parser("accept-invite")
    p_accept.add_argument("conv_name")
    p_accept.add_argument("own_display")
    p_accept.add_argument("peer_name")
    p_accept.add_argument("invite_b64")
    p_accept.set_defaults(func=_action_accept_invite)

    p_send = sub.add_parser("send")
    p_send.add_argument("conv_name")
    p_send.add_argument("text")
    p_send.set_defaults(func=_action_send)

    p_read = sub.add_parser("read")
    p_read.add_argument("conv_name")
    p_read.add_argument("timeout_s", type=float)
    p_read.set_defaults(func=_action_read)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    # Create schema directly via SQLModel.metadata rather than
    # alembic.command.upgrade(). The integration tests run against a
    # disposable KQT_STATE tmpdir, so migration history is irrelevant,
    # and alembic would otherwise: (1) need its script_location config
    # file to be reachable from the installed katzenqt (which is copy-
    # installed into site-packages by `make setup-uv`, not editable),
    # and (2) call asyncio.run internally, which can't nest.
    SQLModel.metadata.create_all(persistent._engine_sync)
    # Give a clean exit on SIGTERM so subprocesses can be killed by the
    # harness if a test's wait loop overruns.
    loop = asyncio.new_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, loop.stop)
    except NotImplementedError:
        pass  # Windows or sandboxed envs
    try:
        return loop.run_until_complete(args.func(args))
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
