"""Action dispatch table for the headless CLI.

Each subparser registered by :func:`_build_parser` binds one async
action function via ``set_defaults(func=...)``. The unified
:func:`katzenqt.headless.cli` entry point looks the action up and
runs it inside its own event loop after ``persistent.init_and_migrate``.

Actions speak a tiny line-oriented protocol so subprocess harnesses
can parse the result without ceremony:

    create-conv CONV_NAME OWN_DISPLAY_NAME
        Create a new Conversation + own ConversationPeer, wait for
        the background provision_read_caps loop to fill in the
        WriteCap/ReadCap via ``ThinClient.new_keypair``, then print
        ``INVITE=<base64>``.

    accept-invite CONV_NAME OWN_DISPLAY_NAME PEER_NAME INVITE_B64
        Create a Conversation + own ConversationPeer, then insert a
        ReadCapWAL from the invite and a ConversationPeer marked
        ``active=True`` so the read loop picks it up. Prints
        ``ACCEPTED``.

    send CONV_NAME TEXT
        Insert a PlaintextWAL via SendOperation; wait until the
        network reports the MixWAL drained (SentLog entry appears).
        Prints ``SENT``.

    multi-send CONV_NAME TEXTS
        ``TEXTS`` is a pipe-separated list. Queue all messages on
        the PWAL up front, then wait for the LAST to land in
        SentLog. Prints ``SENT``.

    read CONV_NAME TIMEOUT_S [EXPECTED_TEXT]
        Poll the ConversationLog until a peer message arrives (any
        whose ConversationPeer is not the own_peer) or the timeout
        fires. Prints ``RECV=<text>`` or ``TIMEOUT``.

    chat-session CONV_NAME STEPS...
        Long-lived session running SEND:<text>, READ:<text>, or
        SLEEP:<seconds> steps against a single connection. Prints
        ``STEP_OK:<n>:...``/``STEP_FAIL:<n>:reason`` per step and
        ``SESSION_DONE`` on clean exit.

    send-file CONV_NAME PATH [--basename X] [--filetype Y]
        Read PATH from disk, wrap it in a GroupChatFileUpload, and
        send. ``--basename`` overrides the on-wire filename;
        ``--filetype`` overrides the MIME-ish tag. Prints ``SENT``.

    read-file CONV_NAME --to-dir DIR [--timeout SEC] [--basename X]
        Poll the ConversationLog for a ``file_marker`` from a peer
        (optionally filtered by basename), copy the on-disk file
        into DIR, verify SHA-256, and print
        ``RECV_FILE=<absolute path>`` or ``TIMEOUT``.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import time
import uuid
from pathlib import Path

import cbor2
from sqlmodel import select

from .. import models, network, persistent


async def _connect_and_start():
    """Connect to kpclientd and kick the background threads running.

    Returns ``(connection, background_task)``. The caller is
    responsible for cancelling the background task on exit. The
    thinclient config path is resolved by
    :func:`katzenqt.network.resolve_thinclient_config`, which
    honours ``$KATZENQT_THINCLIENT_CONFIG``.
    """
    connection = await network.reconnect()
    bg = asyncio.create_task(network.start_background_threads(connection))
    return connection, bg


async def _shutdown(bg):
    network.shutdown()
    try:
        await asyncio.wait_for(bg, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        bg.cancel()


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

    # If a conversation with this name already exists, add the peer to it
    # rather than creating a new conversation (matches the Qt app's
    # accept_invitation flow, which always adds to the selected conv).
    async with persistent.asession() as sess:
        existing = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == args.conv_name
            )
        )).first()

    if existing is not None:
        peer_rcwal = persistent.ReadCapWAL(
            id=uuid.uuid4(),
            read_cap=please_add.read_cap,
            next_index=please_add.read_cap[-104:],
        )
        async with persistent.asession() as sess:
            conv_obj = (await sess.exec(
                select(persistent.Conversation).where(
                    persistent.Conversation.id == existing.id
                )
            )).one()
            peer = persistent.ConversationPeer(
                name=args.peer_name, read_cap_id=peer_rcwal.id, active=True,
                conversation=conv_obj,
            )
            sess.add(peer_rcwal)
            sess.add(peer)
            await sess.commit()
        print("ACCEPTED", flush=True)
        return 0

    # Otherwise, build own conversation + own_peer skeleton.
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


async def _send_one_gcm(conv_name: str, gcm: "models.GroupChatMessage") -> int:
    """Common send path for ``_action_send`` and ``_action_send_file``.

    Serialises ``gcm`` into the conversation's outgoing BACAP stream,
    spawns the headless background loops, and waits for the final
    PlaintextWAL to land in SentLog. The budget scales with the
    number of chunks so a multi-box attachment is given enough time
    to clear (sixty seconds per chunk on the local docker mixnet,
    one-hundred-twenty seconds minimum).
    """
    async with persistent.asession() as sess:
        convo = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == conv_name
            )
        )).first()
        if convo is None:
            print(f"ERROR=conversation {conv_name!r} not found", flush=True)
            return 2
        conversation_id = convo.id
        # Outgoing writes for this conversation go on the write-cap's
        # bacap_stream (i.e., the WriteCapWAL primary key). find_resendable
        # requires the PWAL's bacap_stream to match a fully-provisioned
        # WriteCapWAL, so using e.g. own_peer.read_cap_id silently stalls.
        own_bacap_stream = convo.write_cap

    send_op = models.SendOperation(
        bacap_stream=own_bacap_stream, messages=[gcm],
    )
    new_write_caps, db_entries = send_op.serialize(
        chunk_size=1530, conversation_id=conversation_id,
    )
    final_pwal_id = db_entries[-1].id
    num_pwals = sum(
        1 for e in db_entries if isinstance(e, persistent.PlaintextWAL)
    )

    async with persistent.asession() as sess:
        for cap_uuid in new_write_caps:
            sess.add(persistent.WriteCapWAL(id=cap_uuid))
        for obj in db_entries:
            sess.add(obj)
        await sess.commit()

    budget_s = max(120.0, num_pwals * 60.0)
    connection, bg = await _connect_and_start()
    try:
        await network.check_for_new()
        deadline = asyncio.get_event_loop().time() + budget_s
        while asyncio.get_event_loop().time() < deadline:
            async with persistent.asession() as sess:
                hit = (await sess.exec(
                    select(persistent.SentLog).where(
                        persistent.SentLog.id == final_pwal_id
                    )
                )).first()
                if hit is not None:
                    print("SENT", flush=True)
                    return 0
            await asyncio.sleep(0.25)
        print("ERROR=send timed out waiting for SentLog", flush=True)
        return 3
    finally:
        await _shutdown(bg)


async def _action_send(args):
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8, text=args.text,
    )
    return await _send_one_gcm(args.conv_name, gcm)


async def _action_send_file(args):
    path = Path(args.path)
    if not path.is_file():
        print(f"ERROR=file not found: {path}", flush=True)
        return 2
    data = path.read_bytes()
    if len(data) > network._ATTACHMENT_HARD_CAP:
        print(
            f"ERROR=file exceeds {network._ATTACHMENT_HARD_CAP}-byte cap: "
            f"{len(data)} bytes",
            flush=True,
        )
        return 2
    file_upload = models.GroupChatFileUpload(
        payload=data,
        filetype=args.filetype or "application/octet-stream",
        basename=args.basename or path.name,
    )
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8, file_upload=file_upload,
    )
    return await _send_one_gcm(args.conv_name, gcm)


async def _action_read_file(args):
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

    to_dir = Path(args.to_dir)
    to_dir.mkdir(parents=True, exist_ok=True)
    basename_filter = args.basename

    connection, bg = await _connect_and_start()
    try:
        await network.signal_readables_to_mixwal()
        deadline = asyncio.get_event_loop().time() + args.timeout
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
                if cl.payload[:1] != b"F":
                    continue
                try:
                    decoded = cbor2.loads(cl.payload[1:])
                except Exception:
                    continue
                if not isinstance(decoded, dict):
                    continue
                if decoded.get("kind") != "file_marker":
                    continue
                if basename_filter is not None and decoded.get("basename") != basename_filter:
                    continue
                rel_path = decoded.get("rel_path")
                marker_sha = decoded.get("sha256")
                basename = decoded.get("basename") or "unnamed"
                if not rel_path or not marker_sha:
                    continue
                src_abs = persistent.state_file.parent / rel_path
                if not src_abs.is_file():
                    continue
                file_bytes = src_abs.read_bytes()
                actual_sha = hashlib.sha256(file_bytes).digest()
                if actual_sha != marker_sha:
                    print(
                        f"WARN=sha256 mismatch for {basename}: "
                        f"marker={marker_sha.hex()} actual={actual_sha.hex()}",
                        flush=True,
                    )
                dst_abs = to_dir / basename
                shutil.copyfile(src_abs, dst_abs)
                print(f"RECV_FILE={dst_abs.resolve()}", flush=True)
                return 0
            await asyncio.sleep(0.5)
        print("TIMEOUT", flush=True)
        return 1
    finally:
        await _shutdown(bg)


async def _action_multi_send(args):
    """Queue N messages on the PWAL all at once, then wait for the LAST of
    them to land in SentLog. Approximates a user that typed and pressed
    Enter several times in quick succession before quitting.
    """
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
        own_bacap_stream = convo.write_cap

    texts = args.texts.split("|")
    final_pwal_ids: list = []
    for text in texts:
        gcm = models.GroupChatMessage(
            version=0, membership_hash=b"TODO" * 8, text=text,
        )
        send_op = models.SendOperation(
            bacap_stream=own_bacap_stream, messages=[gcm],
        )
        _, db_entries = send_op.serialize(
            chunk_size=1530, conversation_id=conversation_id,
        )
        final_pwal_ids.append(db_entries[-1].id)
        async with persistent.asession() as sess:
            for obj in db_entries:
                sess.add(obj)
            await sess.commit()

    connection, bg = await _connect_and_start()
    try:
        await network.check_for_new()
        # Wait for the LAST queued PWAL to clear.
        target = final_pwal_ids[-1]
        for _ in range(2400):  # 600s @ 250ms
            async with persistent.asession() as sess:
                hit = (await sess.exec(
                    select(persistent.SentLog).where(persistent.SentLog.id == target)
                )).first()
                if hit is not None:
                    print("SENT", flush=True)
                    return 0
            await asyncio.sleep(0.25)
        print("ERROR=multi-send timed out", flush=True)
        return 3
    finally:
        await _shutdown(bg)


async def _action_chat_session(args):
    """Long-lived session that runs multiple SEND / READ / SLEEP steps in
    ONE subprocess against a shared background-thread ThinClient, then
    cleanly shuts down. The whole point is to exercise multiple in-process
    state transitions before the subprocess exits, so that on the *next*
    subprocess we know the last committed state on disk is the one that
    matters.

    Timeouts (send=300s, read=180s) are shared across steps.
    """
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
        own_bacap_stream = convo.write_cap

    connection, bg = await _connect_and_start()
    try:
        # Kick the read loop once so the session starts polling any peer
        # streams that have advanced since our last incarnation.
        await network.signal_readables_to_mixwal()

        for step_idx, raw in enumerate(args.steps):
            kind, _, payload = raw.partition(":")
            if kind == "SEND":
                gcm = models.GroupChatMessage(
                    version=0, membership_hash=b"TODO" * 8, text=payload,
                )
                send_op = models.SendOperation(
                    bacap_stream=own_bacap_stream, messages=[gcm],
                )
                _, db_entries = send_op.serialize(
                    chunk_size=1530, conversation_id=conversation_id,
                )
                final_pwal_id = db_entries[-1].id
                async with persistent.asession() as sess:
                    for obj in db_entries:
                        sess.add(obj)
                    await sess.commit()
                await network.check_for_new()
                deadline = asyncio.get_event_loop().time() + 300.0
                ok = False
                while asyncio.get_event_loop().time() < deadline:
                    async with persistent.asession() as sess:
                        hit = (await sess.exec(
                            select(persistent.SentLog).where(
                                persistent.SentLog.id == final_pwal_id
                            )
                        )).first()
                        if hit is not None:
                            ok = True
                            break
                    await asyncio.sleep(0.25)
                if not ok:
                    print(f"STEP_FAIL:{step_idx}:send-timeout:{payload}", flush=True)
                    return 3
                print(f"STEP_OK:{step_idx}:SEND:{payload}:ts={time.time():.3f}", flush=True)

            elif kind == "READ":
                # Nudge the read loop in case no event is outstanding.
                await network.signal_readables_to_mixwal()
                deadline = asyncio.get_event_loop().time() + 180.0
                ok = False
                poll_n = 0
                last_count = -1
                while asyncio.get_event_loop().time() < deadline:
                    poll_n += 1
                    async with persistent.asession() as sess:
                        rows = (await sess.exec(
                            select(persistent.ConversationLog).where(
                                persistent.ConversationLog.conversation_id == conversation_id
                            ).where(
                                persistent.ConversationLog.conversation_peer_id != own_peer_id
                            )
                        )).all()
                    if len(rows) != last_count:
                        last_count = len(rows)
                        print(
                            f"STEP_POLL:{step_idx}:poll={poll_n}:row_count={last_count}:"
                            f"conv_id={conversation_id}:own_peer_id={own_peer_id}:looking_for={payload!r}",
                            flush=True,
                        )
                    for cl in rows:
                        if cl.payload[:1] != b"F":
                            continue
                        try:
                            gcm = models.GroupChatMessage.from_cbor(cl.payload[1:])
                        except Exception:
                            continue
                        if gcm.text == payload:
                            ok = True
                            break
                    if ok:
                        break
                    await asyncio.sleep(0.5)
                if not ok:
                    print(f"STEP_FAIL:{step_idx}:read-timeout:{payload}", flush=True)
                    return 4
                print(f"STEP_OK:{step_idx}:READ:{payload}:ts={time.time():.3f}", flush=True)

            elif kind == "SLEEP":
                await asyncio.sleep(float(payload))
                print(f"STEP_OK:{step_idx}:SLEEP:{payload}", flush=True)

            else:
                print(f"STEP_FAIL:{step_idx}:unknown-step:{raw}", flush=True)
                return 5

        print("SESSION_DONE", flush=True)
        return 0
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

    expected = getattr(args, "expected_text", None)
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
                    if not gcm.text:
                        continue
                    if expected is not None and gcm.text != expected:
                        continue
                    print("RECV=" + gcm.text, flush=True)
                    return 0
            await asyncio.sleep(0.5)
        print("TIMEOUT", flush=True)
        return 1
    finally:
        await _shutdown(bg)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="katzenqt-headless",
        description="Headless driver for katzenqt: send/receive over kpclientd.",
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

    p_multi = sub.add_parser("multi-send")
    p_multi.add_argument("conv_name")
    p_multi.add_argument("texts", help="pipe-separated list of texts to queue")
    p_multi.set_defaults(func=_action_multi_send)

    p_read = sub.add_parser("read")
    p_read.add_argument("conv_name")
    p_read.add_argument("timeout_s", type=float)
    p_read.add_argument("expected_text", nargs="?", default=None)
    p_read.set_defaults(func=_action_read)

    p_sess = sub.add_parser("chat-session")
    p_sess.add_argument("conv_name")
    p_sess.add_argument(
        "steps", nargs="+",
        help="steps like SEND:text, READ:text, SLEEP:seconds",
    )
    p_sess.set_defaults(func=_action_chat_session)

    p_send_file = sub.add_parser("send-file")
    p_send_file.add_argument("conv_name")
    p_send_file.add_argument("path", help="path to the file to send")
    p_send_file.add_argument("--basename", default=None)
    p_send_file.add_argument("--filetype", default=None)
    p_send_file.set_defaults(func=_action_send_file)

    p_read_file = sub.add_parser("read-file")
    p_read_file.add_argument("conv_name")
    p_read_file.add_argument(
        "--to-dir", dest="to_dir", required=True,
        help="directory where the received attachment is written",
    )
    p_read_file.add_argument(
        "--timeout", type=float, default=600.0,
        help="seconds to wait for a file_marker to arrive",
    )
    p_read_file.add_argument(
        "--basename", default=None,
        help="restrict to attachments whose basename matches",
    )
    p_read_file.set_defaults(func=_action_read_file)

    return parser
