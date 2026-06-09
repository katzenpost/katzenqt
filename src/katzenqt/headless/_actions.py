"""Action dispatch table for the headless CLI.

Each subparser registered by :func:`_build_parser` binds one async
action function via ``set_defaults(func=...)``. The unified
:func:`katzenqt.headless.cli` entry point looks the action up and
runs it inside its own event loop after ``persistent.init_and_migrate``.

Actions emit their results and diagnostics through the logging
framework; the headless CLI routes logs to stderr, and subprocess
harnesses capture that stream and look for result tokens such as
``VOUCHER=`` or ``SENT``. Contact is established with the Contact
Voucher protocol, see
https://katzenpost.network/docs/specs/contact_voucher/ .

    create-conv CONV_NAME OWN_DISPLAY_NAME
        Create a new Conversation + own ConversationPeer and wait for
        the background provision_read_caps loop to fill in the
        WriteCap/ReadCap via ``ThinClient.new_keypair``. Logs
        ``CREATED``.

    voucher-mint CONV_NAME DISPLAY_NAME
        Joiner side: mint a Voucher over the conversation's MessageStream
        and publish the payload to VoucherStream box 0. Logs
        ``VOUCHER=<base64>`` to hand to an existing member out of band.

    voucher-induct CONV_NAME PEER_NAME VOUCHER_B64
        Inductor side: read the joiner's payload, seal a reply carrying
        the group's read caps, write it back, and add the joiner as an
        active peer. Logs ``INDUCTED=<joiner display name>``.

    voucher-await CONV_NAME
        Joiner side: poll for the inductor's reply, open it, move this
        conversation's write cap onto the salt-mutated sequence, and add
        the members named in the reply as peers. Logs ``JOINED``.

    send CONV_NAME TEXT
        Insert a PlaintextWAL via SendOperation; wait until the
        network reports the MixWAL drained (SentLog entry appears).
        Logs ``SENT``.

    multi-send CONV_NAME TEXTS
        ``TEXTS`` is a pipe-separated list. Queue all messages on
        the PWAL up front, then wait for the LAST to land in
        SentLog. Logs ``SENT``.

    read CONV_NAME TIMEOUT_S [EXPECTED_TEXT]
        Poll the ConversationLog until a peer message arrives (any
        whose ConversationPeer is not the own_peer) or the timeout
        fires. Logs ``RECV=<text>`` or ``TIMEOUT``.

    chat-session CONV_NAME STEPS...
        Long-lived session running SEND:<text>, READ:<text>, or
        SLEEP:<seconds> steps against a single connection. Logs
        ``STEP_OK:<n>:...``/``STEP_FAIL:<n>:reason`` per step and
        ``SESSION_DONE`` on clean exit.

    send-file CONV_NAME PATH [--basename X] [--filetype Y]
        Read PATH from disk, wrap it in a GroupChatFileUpload, and
        send. ``--basename`` overrides the on-wire filename;
        ``--filetype`` overrides the MIME-ish tag. Logs ``SENT``.

    read-file CONV_NAME --to-dir DIR [--timeout SEC] [--basename X]
        Poll the ConversationLog for a ``file_marker`` from a peer
        (optionally filtered by basename), copy the on-disk file
        into DIR, verify SHA-256, and log
        ``RECV_FILE=<absolute path>`` or ``TIMEOUT``.

    info
        Emit one line of JSON describing the state file's current
        alembic revision, the state path, the list of (non-substream)
        conversations and their peer/message counts, and the open WAL
        row counts. Does not contact kpclientd.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from base64 import b64decode, b64encode
from pathlib import Path

import cbor2
import sqlalchemy as sa
from alembic.runtime.migration import MigrationContext
from sqlmodel import select

from .. import models, network, persistent
from ..tally import engine as tally_engine
from ..tally import events as tally_events
from ..tally import schema as tally_schema
from ..tally import send as tally_send
from ..tally import sync as tally_sync
from ..tally.controller import INSTANCE as tally_instance
from ..voucher import await_and_open, derive_read_and_induct, mint_and_publish

logger = logging.getLogger("katzen.headless")


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


async def _shutdown(bg, connection):
    """Tear down a session opened by :func:`_connect_and_start`.

    Sets the network's shutdown event, waits for the background task
    to drain, and then closes the ThinClient so kpclientd receives
    its ``thin_close`` and frees this connection's ARQ state. The
    close is mandatory; see the docstring on
    :func:`katzenqt.headless.stop` for why.
    """
    network.shutdown()
    try:
        await asyncio.wait_for(bg, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        bg.cancel()
    connection.stop()


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
            logger.error("no read_cap provisioned after timeout")
            return 2

        logger.info("CREATED")
        return 0
    finally:
        await _shutdown(bg, connection)


async def _conv_id_by_name(conv_name: str) -> "int | None":
    async with persistent.asession() as sess:
        conv = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == conv_name
            )
        )).first()
        return conv.id if conv is not None else None


async def _wait_for_conv_write_cap(conversation_id: int, attempts: int = 120, delay: float = 0.5) -> bool:
    """Block until provision_read_caps has filled in the conversation's write cap."""
    for _ in range(attempts):
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conversation_id)
            wcw = await sess.get(persistent.WriteCapWAL, conv.write_cap)
            if wcw is not None and wcw.write_cap is not None:
                return True
        await asyncio.sleep(delay)
    return False


async def _action_voucher_mint(args):
    conv_id = await _conv_id_by_name(args.conv_name)
    if conv_id is None:
        logger.error("conversation %r not found", args.conv_name)
        return 2
    connection, bg = await _connect_and_start()
    try:
        if not await _wait_for_conv_write_cap(conv_id):
            logger.error("write cap not provisioned after timeout")
            return 2
        voucher = await mint_and_publish(connection, conv_id, args.display_name)
        logger.info("VOUCHER=%s", b64encode(voucher).decode())
        return 0
    finally:
        await _shutdown(bg, connection)


async def _action_voucher_induct(args):
    conv_id = await _conv_id_by_name(args.conv_name)
    if conv_id is None:
        logger.error("conversation %r not found", args.conv_name)
        return 2
    try:
        voucher = b64decode(args.voucher_b64.strip().encode())
    except Exception:
        logger.error("invalid voucher encoding")
        return 2
    connection, bg = await _connect_and_start()
    try:
        joiner_name = await derive_read_and_induct(
            connection, conv_id, args.peer_name, voucher,
        )
        logger.info("INDUCTED=%s", joiner_name)
        return 0
    finally:
        await _shutdown(bg, connection)


async def _action_voucher_await(args):
    conv_id = await _conv_id_by_name(args.conv_name)
    if conv_id is None:
        logger.error("conversation %r not found", args.conv_name)
        return 2
    connection, bg = await _connect_and_start()
    try:
        await await_and_open(connection, conv_id)
        logger.info("JOINED")
        return 0
    finally:
        await _shutdown(bg, connection)


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
            logger.error("conversation %r not found", conv_name)
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
                    logger.info("SENT")
                    return 0
            await asyncio.sleep(0.25)
        logger.error("send timed out waiting for SentLog")
        return 3
    finally:
        await _shutdown(bg, connection)


async def _action_send(args):
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8, text=args.text,
    )
    return await _send_one_gcm(args.conv_name, gcm)


async def _action_send_file(args):
    path = Path(args.path)
    if not path.is_file():
        logger.error("file not found: %s", path)
        return 2
    data = path.read_bytes()
    if len(data) > network._ATTACHMENT_HARD_CAP:
        logger.error(
            "file exceeds %d-byte cap: %d bytes",
            network._ATTACHMENT_HARD_CAP, len(data),
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
            logger.error("conversation %r not found", args.conv_name)
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
                    logger.warning(
                        "sha256 mismatch for %s: marker=%s actual=%s",
                        basename, marker_sha.hex(), actual_sha.hex(),
                    )
                dst_abs = to_dir / basename
                shutil.copyfile(src_abs, dst_abs)
                logger.info("RECV_FILE=%s", dst_abs.resolve())
                return 0
            await asyncio.sleep(0.5)
        logger.info("TIMEOUT")
        return 1
    finally:
        await _shutdown(bg, connection)


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
            logger.error("conversation %r not found", args.conv_name)
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
                    logger.info("SENT")
                    return 0
            await asyncio.sleep(0.25)
        logger.error("multi-send timed out")
        return 3
    finally:
        await _shutdown(bg, connection)


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
            logger.error("conversation %r not found", args.conv_name)
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
                # Ten minutes: a chat-session shares its kpclientd
                # connection with the test's other concurrent role and
                # the two compete for daemon CPU on a loaded CI runner,
                # which pushes per-step wall time well above the
                # single-role baseline.
                deadline = asyncio.get_event_loop().time() + 600.0
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
                    logger.error(f"STEP_FAIL:{step_idx}:send-timeout:{payload}")
                    return 3
                logger.info(f"STEP_OK:{step_idx}:SEND:{payload}:ts={time.time():.3f}")

            elif kind == "READ":
                # Nudge the read loop in case no event is outstanding.
                await network.signal_readables_to_mixwal()
                # Six minutes: enough for the loaded CI mixnet's
                # propagation-and-poll round trip without giving up
                # on a session that is otherwise progressing. The
                # outer chat-session subprocess timeout in
                # tests/integration/test_restart.py is the harder
                # bound.
                deadline = asyncio.get_event_loop().time() + 360.0
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
                        logger.info(
                            f"STEP_POLL:{step_idx}:poll={poll_n}:row_count={last_count}:"
                            f"conv_id={conversation_id}:own_peer_id={own_peer_id}:looking_for={payload!r}"
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
                    logger.error(f"STEP_FAIL:{step_idx}:read-timeout:{payload}")
                    return 4
                logger.info(f"STEP_OK:{step_idx}:READ:{payload}:ts={time.time():.3f}")

            elif kind == "SLEEP":
                await asyncio.sleep(float(payload))
                logger.info(f"STEP_OK:{step_idx}:SLEEP:{payload}")

            else:
                logger.error(f"STEP_FAIL:{step_idx}:unknown-step:{raw}")
                return 5

        logger.info("SESSION_DONE")
        return 0
    finally:
        await _shutdown(bg, connection)


async def _action_read(args):
    async with persistent.asession() as sess:
        convo = (await sess.exec(
            select(persistent.Conversation).where(
                persistent.Conversation.name == args.conv_name
            )
        )).first()
        if convo is None:
            logger.error("conversation %r not found", args.conv_name)
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
                    logger.info("RECV=%s", gcm.text)
                    return 0
            await asyncio.sleep(0.5)
        logger.info("TIMEOUT")
        return 1
    finally:
        await _shutdown(bg, connection)


async def _action_info(args) -> int:
    """Print one line of JSON describing this state file's schema,
    conversations, and outstanding WAL counts.

    Synthetic substream peers (whose names begin with the
    ``:substream:`` marker) are excluded from per-conversation peer
    counts so the output reflects only user-facing peers.
    """
    with persistent._engine_sync.connect() as conn:
        ctx = MigrationContext.configure(conn)
        revision = ctx.get_current_revision()

    async with persistent.asession() as sess:
        conversations = (await sess.exec(
            select(persistent.Conversation)
        )).all()
        conv_summaries = []
        for c in conversations:
            real_peers = [
                p for p in c.peers
                if not p.name.startswith(network._SUBSTREAM_NAME_PREFIX)
            ]
            msg_count = (await sess.exec(
                select(sa.func.count()).select_from(persistent.ConversationLog).where(
                    persistent.ConversationLog.conversation_id == c.id
                )
            )).one()
            conv_summaries.append({
                "id": c.id,
                "name": c.name,
                "peer_count": len(real_peers),
                "messages": int(msg_count),
            })

        pwal_count = (await sess.exec(
            select(sa.func.count()).select_from(persistent.PlaintextWAL)
        )).one()
        mw_count = (await sess.exec(
            select(sa.func.count()).select_from(persistent.MixWAL)
        )).one()
        rp_count = (await sess.exec(
            select(sa.func.count()).select_from(persistent.ReceivedPiece)
        )).one()

    payload = {
        "alembic_revision": revision,
        "state_file": str(persistent.state_file),
        "conversations": conv_summaries,
        "wal": {
            "plaintext": int(pwal_count),
            "mix": int(mw_count),
            "received_piece": int(rp_count),
        },
    }
    logger.info(json.dumps(payload))
    return 0


def _parse_slot_votes(items: "list[str]") -> "dict[str, str]":
    """Turn ``["s0=yes", "s1=no"]`` into ``{"s0": "yes", "s1": "no"}``."""
    choice = {}
    for item in items:
        slot, sep, avail = item.partition("=")
        if not sep:
            raise ValueError(f"slot vote {item!r} must be SLOT_ID=availability")
        choice[slot] = avail
    return choice


def _tally_json(result: "tally_engine.TallyResult") -> str:
    return json.dumps({
        "survey_id": result.survey_id.hex(),
        "mode": result.mode.value,
        "status": result.status,
        "n_voters": result.n_voters,
        "slots": [
            {"slot_id": s.slot_id, "text": s.text, "yes": s.yes, "maybe": s.maybe, "no": s.no}
            for s in result.slots
        ],
    })


async def _conversation_by_name(sess, conv_name: str):
    return (await sess.exec(
        select(persistent.Conversation).where(persistent.Conversation.name == conv_name)
    )).first()


async def _action_tally_create(args):
    """Create a survey, persist its initial state, and broadcast it. Logs
    ``TALLY_CREATED=<survey_id hex>``."""
    try:
        mode = tally_schema.Mode(args.mode)
    except ValueError:
        logger.error("unknown mode %r", args.mode)
        return 2
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo = await _conversation_by_name(sess, args.conv_name)
        if convo is None:
            logger.error("conversation %r not found", args.conv_name)
            return 2
        doc = await tally_instance.create_local(
            sess, convo, survey_id, args.topic, mode, args.slot,
        )
        blob = tally_sync.full_state(doc)
        await sess.commit()

    rc = await _send_one_gcm(args.conv_name, tally_events.build_create(survey_id, blob))
    if rc == 0:
        logger.info("TALLY_CREATED=%s", survey_id.hex())
    return rc


async def _wait_for_survey(survey_id: bytes, deadline: float) -> bool:
    while asyncio.get_event_loop().time() < deadline:
        async with persistent.asession() as sess:
            if await sess.get(persistent.TallyState, survey_id) is not None:
                return True
        await asyncio.sleep(0.5)
    return False


async def _wait_for_sent(final_pwal_id, deadline: float) -> bool:
    while asyncio.get_event_loop().time() < deadline:
        async with persistent.asession() as sess:
            hit = (await sess.exec(
                select(persistent.SentLog).where(persistent.SentLog.id == final_pwal_id)
            )).first()
            if hit is not None:
                return True
        await asyncio.sleep(0.25)
    return False


async def _action_tally_vote(args):
    """Wait for the survey to arrive, record our own vote, and broadcast it,
    all on a single connection. Logs ``VOTED``.

    The whole sequence must share one ``_connect_and_start`` cycle: a
    ``_shutdown`` sets the network module's global quit event, which would
    stop the loops of any subsequent connection in this process, so we cannot
    await the survey on one connection and send the vote on another.
    """
    survey_id = bytes.fromhex(args.survey)
    try:
        choice = _parse_slot_votes(args.slot)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    connection, bg = await _connect_and_start()
    try:
        await network.signal_readables_to_mixwal()
        if not await _wait_for_survey(
            survey_id, asyncio.get_event_loop().time() + args.timeout,
        ):
            logger.error("survey %s not received within %.0fs", survey_id.hex(), args.timeout)
            return 1

        async with persistent.asession() as sess:
            convo = await _conversation_by_name(sess, args.conv_name)
            if convo is None:
                logger.error("conversation %r not found", args.conv_name)
                return 2
            try:
                applied = await tally_instance.cast_local_vote(sess, convo, survey_id, choice)
            except ValueError as exc:
                logger.error("invalid vote: %s", exc)
                return 2
            if not applied:
                logger.error("could not apply vote to survey %s", survey_id.hex())
                return 1
            final_pwal_id = await tally_send.stage_outbound(
                sess, convo, tally_events.build_vote(survey_id, choice),
            )
            await sess.commit()

        await network.check_for_new()
        if await _wait_for_sent(
            final_pwal_id, asyncio.get_event_loop().time() + max(120.0, args.timeout),
        ):
            logger.info("VOTED")
            return 0
        logger.error("vote send timed out for survey %s", survey_id.hex())
        return 3
    finally:
        await _shutdown(bg, connection)


async def _action_tally_result(args):
    """Run the loops until the survey has at least ``--expect-voters`` voters,
    then emit the derived tally as ``TALLY=<json>``."""
    survey_id = bytes.fromhex(args.survey)
    connection, bg = await _connect_and_start()
    try:
        await network.signal_readables_to_mixwal()
        deadline = asyncio.get_event_loop().time() + args.timeout
        latest = None
        while True:
            async with persistent.asession() as sess:
                row = await sess.get(persistent.TallyState, survey_id)
            if row is not None:
                latest = tally_engine.tally(tally_sync.load_doc(row.doc_state))
                if args.expect_voters is None or latest.n_voters >= args.expect_voters:
                    logger.info("TALLY=%s", _tally_json(latest))
                    return 0
            if asyncio.get_event_loop().time() >= deadline:
                if latest is not None:
                    logger.info("TALLY=%s", _tally_json(latest))
                logger.error("tally result timed out for survey %s", survey_id.hex())
                return 1
            await asyncio.sleep(0.5)
    finally:
        await _shutdown(bg, connection)


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

    p_mint = sub.add_parser("voucher-mint")
    p_mint.add_argument("conv_name")
    p_mint.add_argument("display_name")
    p_mint.set_defaults(func=_action_voucher_mint)

    p_induct = sub.add_parser("voucher-induct")
    p_induct.add_argument("conv_name")
    p_induct.add_argument("peer_name")
    p_induct.add_argument("voucher_b64")
    p_induct.set_defaults(func=_action_voucher_induct)

    p_await = sub.add_parser("voucher-await")
    p_await.add_argument("conv_name")
    p_await.set_defaults(func=_action_voucher_await)

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

    p_info = sub.add_parser(
        "info",
        help="emit one line of JSON describing the state file's "
             "schema, conversations, and outstanding WAL counts",
    )
    p_info.set_defaults(func=_action_info)

    p_tally_create = sub.add_parser("tally-create")
    p_tally_create.add_argument("conv_name")
    p_tally_create.add_argument("topic")
    p_tally_create.add_argument(
        "--mode", choices=["availability", "approval"], default="approval",
    )
    p_tally_create.add_argument(
        "--slot", action="append", required=True,
        help="descriptive text for one slot; repeat for each slot",
    )
    p_tally_create.set_defaults(func=_action_tally_create)

    p_tally_vote = sub.add_parser("tally-vote")
    p_tally_vote.add_argument("conv_name")
    p_tally_vote.add_argument("--survey", required=True, help="survey id in hex")
    p_tally_vote.add_argument(
        "--slot", action="append", required=True,
        help="a per-slot vote SLOT_ID=availability; repeat per slot",
    )
    p_tally_vote.add_argument("--timeout", type=float, default=600.0)
    p_tally_vote.set_defaults(func=_action_tally_vote)

    p_tally_result = sub.add_parser("tally-result")
    p_tally_result.add_argument("conv_name")
    p_tally_result.add_argument("--survey", required=True, help="survey id in hex")
    p_tally_result.add_argument("--expect-voters", type=int, default=None, dest="expect_voters")
    p_tally_result.add_argument("--timeout", type=float, default=600.0)
    p_tally_result.set_defaults(func=_action_tally_result)

    return parser
