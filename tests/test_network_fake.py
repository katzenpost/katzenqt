"""Unit tests for `katzenqt.network` driven through `FakeThinClient`.

These exercise the branches that the docker-mixnet integration tests
cover only incidentally: the drain loops, the resending pipeline, the
provisioning loop, and the error-handling paths inside each. The fake
satisfies the slice of the `ThinClient` contract that `network.py`
actually calls, so the tests touch zero `katzenpost_thinclient` runtime
code, only its exception classes for `except` matching.

The high-level pattern of every test:

1. Insert any rows into `persistent` that `network.py` would otherwise
   build up over a live session.
2. Call the function under test (either directly or by waking the
   relevant module-level event so a background loop drives it).
3. Assert against the DB and against the fake's `call_log`.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import uuid

import cbor2
import pytest
from sqlmodel import select

from katzenpost_thinclient import (
    BACAPDecryptionFailedError,
    BoxIDNotFoundError,
    CourierError,
    CourierInvalidEpochError,
    DatabaseFailureError,
    StartResendingCancelledError,
    ThinClientOfflineError,
    TombstoneError,
)
from katzenpost_thinclient.core import MKEMDecryptionFailedError

from katzenqt import models, network, persistent
from tests.fakes.thinclient import FakeThinClient


def _make_F_payload(text: str = "hello") -> bytes:
    """Return a ``b'F'``-framed CBOR-encoded GroupChatMessage. The new
    receive-side coalescer parses the CBOR; tests that simulate a
    final-message arrival must produce something decodable."""
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"X" * 32, text=text,
    )
    return b"F" + gcm.to_cbor()


async def _poll_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll ``predicate()`` until it returns truthy or ``timeout`` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    tcalls = 0
    while True:
        if await predicate():
            return
        tcalls += 1
        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError(
                f"predicate not satisfied after {tcalls} polls"
            )
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_keypair(fake, seed: bytes = b"\x11" * 32):
    """Return a fresh KeypairResult from the fake. Centralised so the
    tests do not have to import network.create_new_keypair themselves."""
    return await fake.new_keypair(seed)


async def _insert_write_setup(
    fake, *, conv_name: str = "demo", peer_name: str = "self",
    seed: bytes = b"\x33" * 32, active: bool = True,
):
    """Insert the minimal DB rows representing 'we have just created a
    new conversation and provisioned a write keypair'. Returns a dict
    with bacap_stream, write_cap, read_cap, next_index, conversation_id,
    peer_id.

    The default-commit `expire_on_commit=True` would detach attributes
    the moment we leave the session, so the test would face
    DetachedInstanceError on every later read. We snapshot the integer
    IDs into a plain dict while the rows are still attached.
    """
    kp = await _make_keypair(fake, seed=seed)
    bacap_stream = uuid.uuid4()
    async with persistent.asession() as sess:
        wcw = persistent.WriteCapWAL(
            id=bacap_stream,
            write_cap=kp.write_cap,
            next_index=kp.first_message_index,
        )
        rcw = persistent.ReadCapWAL(
            id=bacap_stream,
            write_cap_id=bacap_stream,
            read_cap=kp.read_cap,
            next_index=kp.first_message_index,
        )
        cpeer = persistent.ConversationPeer(
            name=peer_name,
            read_cap_id=bacap_stream,
            active=active,
        )
        sess.add_all([wcw, rcw, cpeer])
        await sess.commit()
        await sess.refresh(cpeer)
        peer_id = cpeer.id
        conv = persistent.Conversation(
            name=conv_name,
            own_peer_id=peer_id,
            write_cap=bacap_stream,
        )
        sess.add(conv)
        await sess.commit()
        await sess.refresh(conv)
        conversation_id = conv.id
        link = persistent.ConversationPeerLink(
            conversation_peer_id=peer_id, conversation_id=conversation_id,
        )
        sess.add(link)
        await sess.commit()
    return {
        "bacap_stream": bacap_stream,
        "write_cap": kp.write_cap,
        "read_cap": kp.read_cap,
        "first_message_index": kp.first_message_index,
        "conversation_id": conversation_id,
        "peer_id": peer_id,
    }


# ---------------------------------------------------------------------------
# Smoke tests for the fake itself
# ---------------------------------------------------------------------------


class TestFakeThinClientSurface:
    @pytest.mark.asyncio
    async def test_new_keypair_round_trip(self, fake_thinclient):
        kp = await fake_thinclient.new_keypair(b"\x05" * 32)
        assert len(kp.write_cap) == 168
        assert len(kp.read_cap) == 136
        assert kp.write_cap[32:] == kp.read_cap
        # Deterministic: identical seed yields identical caps.
        kp2 = await fake_thinclient.new_keypair(b"\x05" * 32)
        assert kp2.write_cap == kp.write_cap

    @pytest.mark.asyncio
    async def test_encrypt_then_start_resending_round_trip(self, fake_thinclient):
        kp = await fake_thinclient.new_keypair(b"\x06" * 32)
        wcr = await fake_thinclient.encrypt_write(
            plaintext=b"hello bob",
            write_cap=kp.write_cap,
            message_box_index=kp.first_message_index,
        )
        resp = await fake_thinclient.start_resending_encrypted_message(
            write_cap=kp.write_cap,
            envelope_descriptor=wcr.envelope_descriptor,
            envelope_hash=wcr.envelope_hash,
            message_ciphertext=wcr.message_ciphertext,
            read_cap=None, message_box_index=None, reply_index=None,
        )
        assert resp.plaintext == b""

        # Now bob reads from the same box id (read_cap) and sees the payload.
        rcr = await fake_thinclient.encrypt_read(
            read_cap=kp.read_cap, message_box_index=kp.first_message_index,
        )
        resp_read = await fake_thinclient.start_resending_encrypted_message(
            read_cap=kp.read_cap,
            write_cap=None,
            message_box_index=kp.first_message_index,
            reply_index=None,
            envelope_descriptor=rcr.envelope_descriptor,
            envelope_hash=rcr.envelope_hash,
            message_ciphertext=rcr.message_ciphertext,
        )
        assert resp_read.plaintext == b"hello bob"

    @pytest.mark.asyncio
    async def test_inject_error_pops_in_fifo(self, fake_thinclient):
        fake_thinclient.inject_error("new_keypair", RuntimeError("first"))
        fake_thinclient.inject_error("new_keypair", RuntimeError("second"))
        with pytest.raises(RuntimeError, match="first"):
            await fake_thinclient.new_keypair(b"\x00" * 32)
        with pytest.raises(RuntimeError, match="second"):
            await fake_thinclient.new_keypair(b"\x00" * 32)
        # Queue exhausted; the next call succeeds.
        kp = await fake_thinclient.new_keypair(b"\x00" * 32)
        assert kp.write_cap


# ---------------------------------------------------------------------------
# Scaffolding helpers for drain_mixwal_{write,read}_single
# ---------------------------------------------------------------------------


async def _set_up_write_flow(
    fake, *, plaintext: bytes = b"Fhello",
    seed: bytes = b"\x33" * 32,
    conv_name: str = "demo",
    peer_name: str = "self",
):
    """Build the DB rows + fake envelope state representing 'we have just
    encrypted a write and persisted it to MixWAL; ready to drain'.

    Returns dict with keys: bacap_stream, write_cap, read_cap,
    first_message_index, conversation_id, peer_id, mw_id, pwal_id.
    """
    # active=False mirrors production: the own peer of a stream we write
    # is never read back (katzen.py's "we are not reading from ourself").
    # An active self peer lets a concurrently running readables_to_mixwal
    # stage a read-MixWAL for this same stream between our two commits,
    # and the write-MixWAL below then violates UNIQUE(bacap_stream).
    setup = await _insert_write_setup(
        fake, seed=seed, conv_name=conv_name, peer_name=peer_name,
        active=False,
    )
    # Encrypt write through the fake so the envelope_hash is recognised.
    wcr = await fake.encrypt_write(
        plaintext=plaintext,
        write_cap=setup["write_cap"],
        message_box_index=setup["first_message_index"],
    )
    pwal_id = uuid.uuid4()
    mw_id = uuid.uuid4()
    courier_dest = fake.couriers[0][0]
    async with persistent.asession() as sess:
        pwal = persistent.PlaintextWAL(
            id=pwal_id,
            bacap_stream=setup["bacap_stream"],
            conversation_id=setup["conversation_id"],
            bacap_payload=plaintext,
        )
        mw = persistent.MixWAL(
            id=mw_id,
            plaintextwal=pwal_id,
            bacap_stream=setup["bacap_stream"],
            envelope_hash=wcr.envelope_hash,
            destination=courier_dest,
            encrypted_payload=wcr.message_ciphertext,
            envelope_descriptor=wcr.envelope_descriptor,
            current_message_index=setup["first_message_index"],
            next_message_index=wcr.next_message_box_index,
            is_read=False,
        )
        sess.add_all([pwal, mw])
        await sess.commit()
    setup.update({"mw_id": mw_id, "pwal_id": pwal_id, "wcr": wcr})
    return setup


async def _set_up_read_flow(fake, *, plaintext: "bytes | None" = None, **insert_kwargs):
    if plaintext is None:
        plaintext = _make_F_payload("hello")
    """Like _set_up_write_flow but also pre-stores the box and builds a
    read-MixWAL row so drain_mixwal_read_single can be tested.
    """
    setup = await _insert_write_setup(fake, **insert_kwargs)
    fake.pre_store(
        write_cap=setup["write_cap"],
        message_box_index=setup["first_message_index"],
        plaintext=plaintext,
    )
    rcr = await fake.encrypt_read(
        read_cap=setup["read_cap"],
        message_box_index=setup["first_message_index"],
    )
    mw_id = uuid.uuid4()
    courier_dest = fake.couriers[0][0]
    async with persistent.asession() as sess:
        mw = persistent.MixWAL(
            id=mw_id,
            plaintextwal=None,
            bacap_stream=setup["bacap_stream"],
            envelope_hash=rcr.envelope_hash,
            destination=courier_dest,
            encrypted_payload=rcr.message_ciphertext,
            envelope_descriptor=rcr.envelope_descriptor,
            current_message_index=setup["first_message_index"],
            next_message_index=rcr.next_message_box_index,
            is_read=True,
        )
        sess.add(mw)
        await sess.commit()
    setup.update({"mw_id": mw_id, "rcr": rcr})
    return setup


# ---------------------------------------------------------------------------
# drain_mixwal_write_single
# ---------------------------------------------------------------------------


class TestDrainMixwalWriteSingle:
    @pytest.mark.asyncio
    async def test_success_marks_sent_and_clears_state(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        draining: set = {setup["bacap_stream"]}
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        # MixWAL and PlaintextWAL gone, SentLog has one row.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            assert await sess.get(persistent.PlaintextWAL, setup["pwal_id"]) is None
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 1
        # bacap_stream released from the in-progress set.
        assert setup["bacap_stream"] not in draining
        # Fake recorded the start_resending call with the right envelope hash.
        last = fake_thinclient.last_call("start_resending_encrypted_message")
        assert last["envelope_hash"] == setup["wcr"].envelope_hash
        assert last["write_cap"] == setup["write_cap"]

    @pytest.mark.asyncio
    async def test_offline_error_is_swallowed(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", ThinClientOfflineError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        # MixWAL retained, no SentLog row produced.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 0
        # The stream is released instead of stranded in draining_right_now
        # so the drain loop re-schedules it after the connection returns.
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_broken_pipe_is_swallowed(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", BrokenPipeError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_cancelled_is_swallowed(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", StartResendingCancelledError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_transient_sqlite_busy_on_ack_mark_is_retried(
        self, fake_thinclient, monkeypatch,
    ):
        from sqlalchemy.exc import OperationalError

        setup = await _set_up_write_flow(fake_thinclient)
        orig_mark_sent = persistent.SentLog.mark_sent
        fail = {"armed": True}

        async def flaky_mark_sent(connection, mw, resend_queue, **kwargs):
            if fail["armed"]:
                fail["armed"] = False
                raise OperationalError(
                    "INSERT INTO sentlog", {}, Exception("database is locked"),
                )
            return await orig_mark_sent(connection, mw, resend_queue, **kwargs)

        monkeypatch.setattr(persistent.SentLog, "mark_sent", flaky_mark_sent)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        mixwal_updated = getattr(network, "__mixwal_updated")
        mixwal_updated.clear()
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        # The ACK was not consumed: MW and no SentLog yet, and the stream
        # is handed back so the drain loop's next pass retries. give_up also
        # pokes __mixwal_updated so the retry is prompt rather than waiting
        # the drain loop's 15s sweep.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            assert (await sess.exec(select(persistent.SentLog))).all() == []
        assert setup["bacap_stream"] not in draining
        assert mixwal_updated.is_set()
        # A fresh pass (unpatched) finalizes the ACK.
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, set(),
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            assert len((await sess.exec(select(persistent.SentLog))).all()) == 1

    @pytest.mark.asyncio
    async def test_stale_ack_finalizes_delivered_message(self, fake_thinclient):
        """A write drain that died mid-commit leaves no later MW, so a
        stale ACK (wcw.next_index already past this MW's next index) must
        still finalize the message it delivered: PWAL to SentLog, log row
        to sent, both WALs reaped."""
        setup = await _set_up_write_flow(fake_thinclient)
        async with persistent.asession() as sess:
            # Simulate a later, already-ACKed message having advanced the
            # writer to (at least) this MW's next index.
            wcw = await sess.get(persistent.WriteCapWAL, setup["bacap_stream"])
            wcw.next_index = setup["wcr"].next_message_box_index
            sess.add(wcw)
            cl = persistent.ConversationLog(
                conversation_id=setup["conversation_id"],
                conversation_peer_id=setup["peer_id"],
                conversation_order=0,
                payload=b"Fhello",
                network_status=1,
                outgoing_pwal=setup["pwal_id"],
            )
            sess.add(cl)
            await sess.commit()
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            # MW and PWAL reaped, SentLog row written, convlog flipped to sent.
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            assert await sess.get(persistent.PlaintextWAL, setup["pwal_id"]) is None
            sent = await sess.get(persistent.SentLog, setup["pwal_id"])
            assert sent is not None
            cl = (await sess.exec(select(persistent.ConversationLog))).one()
            assert cl.network_status == 2
            # The writer index must NOT be regressed.
            wcw = await sess.get(persistent.WriteCapWAL, setup["bacap_stream"])
            assert wcw.next_index == setup["wcr"].next_message_box_index

    @pytest.mark.asyncio
    async def test_duplicate_ack_is_idempotent(self, fake_thinclient):
        """A duplicate ACK — SentLog row already written while the PWAL is
        still present — must not wedge the drain: mark_sent reuses the row
        instead of re-inserting it (no IntegrityError on the SentLog primary
        key, which the branch's OperationalError-only handler would not
        catch), and the drain still finalizes the stream."""
        setup = await _set_up_write_flow(fake_thinclient)
        async with persistent.asession() as sess:
            sess.add(persistent.SentLog(id=setup["pwal_id"]))
            await sess.commit()
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        async with persistent.asession() as sess:
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 1 and sent[0].id == setup["pwal_id"]
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            assert await sess.get(persistent.PlaintextWAL, setup["pwal_id"]) is None
            wcw = await sess.get(persistent.WriteCapWAL, setup["bacap_stream"])
            assert wcw.next_index == setup["wcr"].next_message_box_index
        assert setup["bacap_stream"] not in draining

    def test_write_done_callback_releases_stream_on_failure(self):
        """The fire-and-forget write drain's done-callback must release a
        stranded stream when the task dies with an exception (anything that
        is not the swallowed OperationalError) and poke the drain loop so
        the MixWAL is re-dispatched promptly."""

        class _FailedTask:
            def cancelled(self):
                return False

            def exception(self):
                return RuntimeError("boom")

        class _CancelledTask:
            def cancelled(self):
                return True

            def exception(self):
                return None

        draining: set = {"stream-1", "stream-2"}
        mixwal_updated = getattr(network, "__mixwal_updated")
        mixwal_updated.clear()
        network._on_write_done(_FailedTask(), "stream-1", draining)
        assert "stream-1" not in draining
        assert mixwal_updated.is_set()
        network._on_write_done(_CancelledTask(), "stream-2", draining)
        assert "stream-2" not in draining

    @pytest.mark.asyncio
    async def test_conv_id_propagates_when_pwal_has_convlog(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient, plaintext=b"Fhi")
        # Add a ConversationLog row tied to the outgoing pwal so
        # mark_sent returns its conversation id.
        async with persistent.asession() as sess:
            cl = persistent.ConversationLog(
                conversation_id=setup["conversation_id"],
                conversation_peer_id=setup["peer_id"],
                conversation_order=0,
                payload=b"Fhi",
                network_status=1,
                outgoing_pwal=setup["pwal_id"],
            )
            sess.add(cl)
            await sess.commit()
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        # Drain queue should receive (conv_id, True).
        # Drain it after so we know it was put.
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        # Give the create_task a beat to run.
        await asyncio.sleep(0)
        # The conversation_update_queue is an asyncio.Queue; pop one.
        assert network.conversation_update_queue.qsize() >= 1
        first = await network.conversation_update_queue.get()
        assert first == (setup["conversation_id"], True)

    @pytest.mark.asyncio
    async def test_counter_probe_interrupted_by_reconnect_gives_up(
        self, fake_thinclient, monkeypatch,
    ):
        monkeypatch.setattr(network, "_RECONNECT_GRACE_SECONDS", 0.05)
        """A reconnect mid-`get_message_box_index_counter` must release the
        stream and leave the MixWAL row for an idempotent re-send."""
        setup = await _set_up_write_flow(fake_thinclient)
        probe_started = asyncio.Event()
        held = asyncio.Event()

        async def hang_counter(message_box_index):
            probe_started.set()
            await held.wait()  # never set in this test
            raise AssertionError("unreachable")

        monkeypatch.setattr(
            fake_thinclient, "get_message_box_index_counter", hang_counter,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        async def simulate_reconnect():
            # Fire the reconnect only once the RPC is in flight:
            # on_connection_status(True) swaps in a fresh event, making the
            # race moot if it fires first.
            await asyncio.wait_for(probe_started.wait(), timeout=5.0)
            await network.on_connection_status({"is_connected": False, "err": None})
            await network.on_connection_status({"is_connected": True, "err": None})

        reconnector = asyncio.create_task(simulate_reconnect())
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        await reconnector
        assert setup["bacap_stream"] not in draining
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 1
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_resend_interrupted_by_reconnect_gives_up(self, fake_thinclient, monkeypatch):
        monkeypatch.setattr(network, "_RECONNECT_GRACE_SECONDS", 0.05)
        """A reconnect mid-`start_resending_encrypted_message` must release the
        stream for an idempotent re-send without touching mark_sent."""
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.hold_ack(setup["wcr"].envelope_hash)
        resend_started = asyncio.Event()
        orig_resend = fake_thinclient.start_resending_encrypted_message

        async def held_resend(
            read_cap: "bytes | None" = None,
            write_cap: "bytes | None" = None,
            message_box_index: "bytes | None" = None,
            reply_index: "int | None" = None,
            envelope_descriptor: "bytes | None" = None,
            message_ciphertext: "bytes | None" = None,
            envelope_hash: "bytes | None" = None,
            no_retry_on_box_id_not_found: bool = False,
            no_idempotent_box_already_exists: bool = False,
            **kwargs,
        ):
            resend_started.set()
            return await orig_resend(
                read_cap=read_cap,
                write_cap=write_cap,
                message_box_index=message_box_index,
                reply_index=reply_index,
                envelope_descriptor=envelope_descriptor,
                message_ciphertext=message_ciphertext,
                envelope_hash=envelope_hash,
                no_retry_on_box_id_not_found=no_retry_on_box_id_not_found,
                no_idempotent_box_already_exists=no_idempotent_box_already_exists,
                **kwargs,
            )

        monkeypatch.setattr(
            fake_thinclient, "start_resending_encrypted_message", held_resend,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        async def simulate_reconnect():
            # Fire the reconnect only once the resend is in flight
            # (marker-swap caveat as above).
            await asyncio.wait_for(resend_started.wait(), timeout=5.0)
            await network.on_connection_status({"is_connected": False, "err": None})
            await network.on_connection_status({"is_connected": True, "err": None})

        reconnector = asyncio.create_task(simulate_reconnect())
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        await reconnector
        assert setup["bacap_stream"] not in draining
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            assert (await sess.exec(select(persistent.SentLog))).all() == []

    @pytest.mark.asyncio
    async def test_ack_bookkeeping_interrupted_by_reconnect_gives_up(
        self, fake_thinclient, monkeypatch,
    ):
        monkeypatch.setattr(network, "_RECONNECT_GRACE_SECONDS", 0.05)
        """A reconnect mid-mark_sent must leave the MW for the next pass;
        a later un-hanging mark_sent still finalizes the ACK."""
        setup = await _set_up_write_flow(fake_thinclient, plaintext=b"Fhello")
        held = asyncio.Event()
        mark_sent_started = asyncio.Event()
        calls = {"n": 0}
        orig_counter = fake_thinclient.get_message_box_index_counter

        async def hang_after_probe(message_box_index):
            calls["n"] += 1
            if calls["n"] > 1:  # first call is the drain's own probe
                mark_sent_started.set()
                await held.wait()  # mark_sent's counter RPC
            return await orig_counter(message_box_index)

        monkeypatch.setattr(
            fake_thinclient, "get_message_box_index_counter", hang_after_probe,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        async def simulate_reconnect():
            # Fire the reconnect only once mark_sent's RPC is in flight
            # (marker-swap caveat as above).
            await asyncio.wait_for(mark_sent_started.wait(), timeout=5.0)
            await network.on_connection_status({"is_connected": False, "err": None})
            await network.on_connection_status({"is_connected": True, "err": None})

        reconnector = asyncio.create_task(simulate_reconnect())
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, draining,
        )
        await reconnector
        # give_up: MW kept, stream released, no SentLog yet.
        assert setup["bacap_stream"] not in draining
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            assert (await sess.exec(select(persistent.SentLog))).all() == []
        # Un-hang the in-flight call; it finalizes the ACK and prunes the MW.
        held.set()

        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            assert (await sess.exec(select(persistent.SentLog))).all() == []
        draining.add(setup["bacap_stream"])
        await network.drain_mixwal_write_single(fake_thinclient, mw, draining)

        async def finalized():
            async with persistent.asession() as sess:
                mw_gone = await sess.get(persistent.MixWAL, setup["mw_id"]) is None
                sled = (await sess.exec(select(persistent.SentLog))).all()
                return mw_gone and len(sled) >= 1

        try:
            await asyncio.wait_for(_poll_until(finalized), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("mark_sent did not finalize the ACK after un-hanging")

    @pytest.mark.asyncio
    async def test_courier_invalid_epoch_remints_write(self, fake_thinclient):
        """A stale-epoch rejection of a write re-mints from the retained
        PlaintextWAL payload at the same index, then the next drain
        completes the send."""
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("stale"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        getattr(network, "__mixwal_updated").clear()
        await network.drain_mixwal_write_single(fake_thinclient, mw, draining)
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash != setup["wcr"].envelope_hash
            assert row.current_message_index == setup["first_message_index"]
            assert await sess.get(persistent.PlaintextWAL, setup["pwal_id"]) is not None
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 0
        assert fake_thinclient.call_count("encrypt_write") == 2
        last = fake_thinclient.last_call("encrypt_write")
        assert last["plaintext"] == b"Fhello"
        assert last["write_cap"] == setup["write_cap"]
        assert last["message_box_index"] == setup["first_message_index"]
        assert setup["bacap_stream"] not in draining
        assert getattr(network, "__mixwal_updated").is_set()

        # Second drain: the re-minted envelope goes through.
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            assert await sess.get(persistent.PlaintextWAL, setup["pwal_id"]) is None
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_write_remint_failure_keeps_row(self, fake_thinclient):
        """A failed re-mint must leave the stored envelope alone and still
        hand the stream back through give_up(), which always signals the
        scheduler. The hot-retry guard is give_up()'s 5 s backoff, not a
        withheld signal."""
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("stale"),
        )
        fake_thinclient.inject_error("encrypt_write", ThinClientOfflineError())
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        getattr(network, "__mixwal_updated").clear()
        await network.drain_mixwal_write_single(fake_thinclient, mw, draining)
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash == setup["wcr"].envelope_hash
            assert row.encrypted_payload == setup["wcr"].message_ciphertext
        assert setup["bacap_stream"] not in draining
        assert getattr(network, "__mixwal_updated").is_set()

    @pytest.mark.asyncio
    async def test_generic_courier_error_releases_write_stream(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", CourierError("boom"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_write_single(fake_thinclient, mw, draining)
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash == setup["wcr"].envelope_hash
            sent = (await sess.exec(select(persistent.SentLog))).all()
            assert len(sent) == 0
        assert fake_thinclient.call_count("encrypt_write") == 1
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_missing_plaintextwal_drops_the_row(self, fake_thinclient, caplog):
        """A write MixWAL whose PlaintextWAL vanished can never be re-minted.
        Drop it and shout at CRITICAL: bacap_stream is unique, so keeping it
        would block every later write on the stream. ConversationLog stays
        pending, so the message is not silently marked sent."""
        setup = await _set_up_write_flow(fake_thinclient)
        async with persistent.asession() as sess:
            pwal = await sess.get(persistent.PlaintextWAL, setup["pwal_id"])
            await sess.delete(pwal)
            await sess.commit()
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("stale"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        with caplog.at_level(logging.CRITICAL, logger="katzen.network"):
            await network.drain_mixwal_write_single(fake_thinclient, mw, draining)
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert all(e.network_status != 2 for e in log), "marked sent despite never sending"
        assert setup["bacap_stream"] not in draining
        assert fake_thinclient.call_count("encrypt_write") == 1
        assert setup["bacap_stream"] not in draining


@pytest.mark.asyncio
async def test_reconnect_marker_swap_sets_the_old_and_leaves_the_new_unset():
    """The swap-on-transition pattern _rpc_racing_connection_life callers
    rely on: on_connection_status sets the previously-captured Event and
    replaces the module global with a fresh, unset one. A caller that
    captures the marker once (e.g. at the top of a function covering two
    sequential RPC races) and reuses that same reference for a later race
    would see it as permanently "already done" after a reconnect --
    re-reading the current global right before each race is what avoids
    that (see drain_mixwal_read_single's second race)."""
    network._last_connected = None
    stale = network._reconnect_event
    assert not stale.is_set()

    await network.on_connection_status({"is_connected": False, "err": None})
    await network.on_connection_status({"is_connected": True, "err": None})

    assert stale.is_set(), "the captured-before-swap reference must be set"
    assert network._reconnect_event is not stale
    assert not network._reconnect_event.is_set(), (
        "a fresh read of the global must NOT see it as already reconnected"
    )


@pytest.mark.asyncio
async def test_epoch_marker_swap_sets_the_old_and_leaves_the_new_unset():
    """Same swap-on-transition pattern as _reconnect_event, for PKI epoch
    rollovers via on_new_pki_document."""
    network._last_epoch = None
    stale = network._epoch_event
    assert not stale.is_set()

    await network.on_new_pki_document({"payload": cbor2.dumps({"Epoch": 1})})

    assert stale.is_set(), "the captured-before-swap reference must be set"
    assert network._epoch_event is not stale
    assert not network._epoch_event.is_set(), (
        "a fresh read of the global must NOT see it as already rolled over"
    )

# ---------------------------------------------------------------------------
# drain_mixwal_read_single
# ---------------------------------------------------------------------------


def _make_F_file_payload(blob: bytes, basename: str = "blob.bin",
                          filetype: str = "application/octet-stream") -> bytes:
    """``b'F'``-framed GroupChatMessage carrying a small file_upload that
    fits in a single BACAP box."""
    gcm = models.GroupChatMessage(
        version=0, membership_hash=b"Y" * 32,
        file_upload=models.GroupChatFileUpload(
            payload=blob, filetype=filetype, basename=basename,
        ),
    )
    return b"F" + gcm.to_cbor()


class TestDrainMixwalReadSingle:
    @pytest.mark.asyncio
    async def test_success_with_final_prefix(self, fake_thinclient):
        payload = _make_F_payload("payload")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
        )
        # MW deleted, the coalescer turned the single 'F' piece into one
        # ConversationLog row and pruned the piece, RCW.next_index
        # advanced.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1 and log[0].payload == payload
            pieces = (await sess.exec(select(persistent.ReceivedPiece))).all()
            assert pieces == []
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["rcr"].next_message_box_index
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_tally_event_logs_a_row_and_notifies_the_gui(self, fake_thinclient):
        # A received tally create becomes a ConversationLog row (the timeline
        # shows it) and must push the conversation onto tally_update_queue
        # *after* the consume-commit, so the GUI repaints against committed
        # TallyState.
        from katzenqt import conversation_handlers
        from katzenqt.tally import events, schema, sync
        from katzenqt.tally.engine import Mode

        conversation_handlers.tally_controller.INSTANCE._docs.clear()
        survey_id = uuid.uuid4().bytes
        blob = sync.full_state(
            schema.new_survey_doc(survey_id, "who's up", Mode.APPROVAL, ["monday"])
        )
        payload = b"F" + events.build_create(survey_id, blob).to_cbor()
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            surveys = (await sess.exec(select(persistent.TallyState))).all()
            assert len(surveys) == 1 and surveys[0].survey_id == survey_id
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1
        assert network.tally_update_queue.qsize() == 1
        assert await network.tally_update_queue.get() == setup["conversation_id"]

    @pytest.mark.asyncio
    async def test_lost_read_reply_is_recovered_by_watchdog(self, fake_thinclient):
        # A lost read reply (thinclient query-id no-listener drop, e.g. after a
        # daemon reconnect/replay) must not strand the read forever: the
        # watchdog aborts the in-flight ARQ and releases the stream so the
        # drain loop re-casts the same box with a fresh query id.
        payload = _make_F_payload("hang then recover")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        fake_thinclient.hold_ack_for_box(setup["read_cap"], setup["first_message_index"])
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
            read_watchdog_s=0.05,
        )
        fake_thinclient.last_call("cancel_resending_encrypted_message")
        # Watchdog path is non-destructive: the box is left for a re-cast.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["first_message_index"]
            assert (await sess.exec(select(persistent.ConversationLog))).all() == []
        assert setup["bacap_stream"] not in draining
        # A later pass (un-stuck) completes the read normally. The real
        # drain loop re-adds the stream to draining_right_now before
        # dispatching a new read task; do the same here so the closing
        # assertion actually exercises the success path's own discard,
        # rather than trivially passing because give_up() already emptied
        # the set above.
        draining.add(setup["bacap_stream"])
        fake_thinclient.release_ack_for_box(setup["read_cap"], setup["first_message_index"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1 and log[0].payload == payload
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["rcr"].next_message_box_index
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    @pytest.mark.real_sleeps
    async def test_lost_read_reply_is_recovered_after_reconnect(self, fake_thinclient, caplog):
        # real_sleeps: otherwise the reconnect fires before the read arms and
        # this passes on read_watchdog_s.
        # A reconnect mid-wait is the one concrete signal that a reply could
        # have been orphaned (kpclientd's reconnect-replay delivering to a
        # query_id whose original listener already gave up); the watchdog
        # should give up promptly after observing one, well before the
        # (much larger, and here never reached) flat backstop.
        # Redundant with conftest._reset_network_module_state (which now
        # nulls _last_connected before every test); kept defensively: it
        # makes this test's precondition locally obvious.
        network._last_connected = None
        payload = _make_F_payload("hang then reconnect")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        fake_thinclient.hold_ack_for_box(setup["read_cap"], setup["first_message_index"])
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        async def simulate_reconnect():
            await asyncio.sleep(0.02)
            await network.on_connection_status({"is_connected": False, "err": None})
            await network.on_connection_status({"is_connected": True, "err": None})

        reconnector = asyncio.ensure_future(simulate_reconnect())
        caplog.set_level(logging.WARNING, logger="katzen.network")
        started = asyncio.get_running_loop().time()
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
            read_watchdog_s=60.0,
            reconnect_grace_s=0.05,
        )
        elapsed = asyncio.get_running_loop().time() - started
        await reconnector
        fake_thinclient.last_call("cancel_resending_encrypted_message")
        assert setup["bacap_stream"] not in draining
        assert any("daemon reconnected mid-" in r.message for r in caplog.records), (
            f"recovered some other way after {elapsed:.1f}s: "
            f"{[r.message for r in caplog.records]}"
        )
        assert elapsed < 30.0, f"took {elapsed:.1f}s, so this was read_watchdog_s"

    @pytest.mark.asyncio
    @pytest.mark.real_sleeps
    async def test_lost_read_reply_is_recovered_after_epoch_rollover(self, fake_thinclient, caplog):
        # real_sleeps: otherwise the rollover fires before the read arms and
        # this passes on read_watchdog_s.
        # A PKI epoch rollover mid-wait makes start_resending_encrypted_message's
        # envelope stale for the courier; the watchdog should notice via
        # on_new_pki_document and give up promptly, same as a reconnect.
        payload = _make_F_payload("hang then epoch roll")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        fake_thinclient.hold_ack_for_box(setup["read_cap"], setup["first_message_index"])
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        def _pki_event(epoch: int) -> "dict":
            return {"payload": cbor2.dumps({"Epoch": epoch})}

        async def simulate_epoch_rollover():
            await asyncio.sleep(0.02)
            await network.on_new_pki_document(_pki_event(1))
            await network.on_new_pki_document(_pki_event(2))

        roller = asyncio.ensure_future(simulate_epoch_rollover())
        caplog.set_level(logging.WARNING, logger="katzen.network")
        started = asyncio.get_running_loop().time()
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
            read_watchdog_s=60.0,
            reconnect_grace_s=0.05,
        )
        elapsed = asyncio.get_running_loop().time() - started
        await roller
        fake_thinclient.last_call("cancel_resending_encrypted_message")
        assert setup["bacap_stream"] not in draining
        assert any("PKI epoch rolled over mid-" in r.message for r in caplog.records), (
            f"recovered some other way after {elapsed:.1f}s: "
            f"{[r.message for r in caplog.records]}"
        )
        assert elapsed < 30.0, f"took {elapsed:.1f}s, so this was read_watchdog_s"

    @pytest.mark.asyncio
    async def test_read_re_encrypts_a_fresh_envelope_every_call(self, fake_thinclient):
        # The epoch-rollover fix depends on this: a retried read must never
        # reuse the persisted (potentially stale) envelope on the MixWAL
        # row, or give_up()-then-retry after a rollover would just resend
        # the same now-stale envelope forever.
        payload = _make_F_payload("fresh envelope each time")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now={setup["bacap_stream"]},
        )
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 1
        used_envelope_hash = fake_thinclient.last_call("start_resending_encrypted_message")["envelope_hash"]
        assert used_envelope_hash != setup["rcr"].envelope_hash

    @pytest.mark.asyncio
    async def test_transient_sqlite_busy_on_read_commit_is_retried(
        self, fake_thinclient, monkeypatch,
    ):
        from sqlalchemy.exc import OperationalError
        from sqlmodel.ext.asyncio.session import AsyncSession

        payload = _make_F_payload("retry me")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        orig_commit = AsyncSession.commit
        fail = {"armed": True}

        async def flaky_commit(self):
            if fail["armed"]:
                fail["armed"] = False
                raise OperationalError(
                    "INSERT", {}, Exception("database is locked"),
                )
            return await orig_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", flaky_commit)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
        )
        # The failed transaction rolled back wholesale: no MW deletion, no
        # index advance, no log row, no stray piece — and the stream is
        # released rather than stranded in draining_right_now.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["first_message_index"]
            assert (await sess.exec(select(persistent.ReceivedPiece))).all() == []
            assert (await sess.exec(select(persistent.ConversationLog))).all() == []
        assert setup["bacap_stream"] not in draining
        # A later pass (unpatched) commits the message normally.
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=set(),
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1 and log[0].payload == payload
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["rcr"].next_message_box_index

    @pytest.mark.asyncio
    async def test_single_box_file_upload_spills_to_disk(self, fake_thinclient):
        """A single-box GroupChatMessage carrying a file_upload must
        result in (a) the bytes written under the attachments dir,
        (b) the ConversationLog payload becoming a ``file_marker``
        rather than the raw CBOR, and (c) the SHA-256 in the marker
        matching the file on disk."""
        import cbor2
        import hashlib

        blob = b"sample bytes 0123456789" * 5
        plaintext = _make_F_file_payload(blob, basename="hello.bin")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=plaintext)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1
        payload = log[0].payload
        assert payload[:1] == b"F"
        marker = cbor2.loads(payload[1:])
        assert marker["kind"] == "file_marker"
        assert marker["basename"] == "hello.bin"
        assert marker["size"] == len(blob)
        assert marker["sha256"] == hashlib.sha256(blob).digest()
        from pathlib import Path
        spilled = persistent.state_file.parent / marker["rel_path"]
        assert spilled.is_file()
        assert spilled.read_bytes() == blob

    def test_spill_attachment_is_idempotent_across_retries(self):
        # A retried commit (sqlite lock contention) calls _spill_attachment
        # again with the same content; it must reuse the same file rather
        # than writing (and leaking) a second copy under a fresh name.
        from katzenqt import models

        blob = b"same content, spilled twice" * 3
        file_upload = models.GroupChatFileUpload(
            basename="dup.bin", filetype="arbitrary", payload=blob,
        )
        marker1 = network._spill_attachment(file_upload, b"m" * 32, 4242)
        marker2 = network._spill_attachment(file_upload, b"m" * 32, 4242)
        import cbor2
        m1 = cbor2.loads(marker1[1:])
        m2 = cbor2.loads(marker2[1:])
        assert m1["rel_path"] == m2["rel_path"]
        conv_dir = network._attachments_root() / "4242"
        assert len(list(conv_dir.iterdir())) == 1

    @pytest.mark.asyncio
    async def test_oversized_file_yields_oversized_marker(self, fake_thinclient, monkeypatch):
        """An assembled attachment larger than the hard cap must be
        replaced with a ``file_oversized`` marker and not touch disk."""
        import cbor2
        # Lower the cap so we don't actually have to allocate 200 MiB.
        monkeypatch.setattr(network, "_ATTACHMENT_HARD_CAP", 100)
        blob = b"X" * 4096  # > cap
        plaintext = _make_F_file_payload(blob, basename="big.bin")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=plaintext)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            log = (await sess.exec(select(persistent.ConversationLog))).all()
        assert len(log) == 1
        marker = cbor2.loads(log[0].payload[1:])
        assert marker["kind"] == "file_oversized"
        assert marker["size"] == len(blob)

    @pytest.mark.asyncio
    async def test_continuation_prefix(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Cchunk")
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            pieces = (await sess.exec(select(persistent.ReceivedPiece))).all()
            assert pieces[0].chunk_type == b"C"

    @pytest.mark.asyncio
    async def test_indirection_prefix(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Iredirect")
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            pieces = (await sess.exec(select(persistent.ReceivedPiece))).all()
            assert pieces[0].chunk_type == b"I"

    @pytest.mark.asyncio
    async def test_extended_i_chunk_creates_substream_with_total(
        self, fake_thinclient,
    ):
        """Receive side: a 140-byte I-chunk (b'I' + 4-byte BE
        total + 136-byte read cap) must spawn a substream ReadCapWAL that
        carries the total and a substream peer, and fire a ``started``
        event carrying the conversation id, total, and parent peer name."""
        stub_read_cap = b"\xee" * 136
        plaintext = b"I" + (7).to_bytes(4, "big") + stub_read_cap
        assert len(plaintext) == 141
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=plaintext, peer_name="parent_alice",
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            substreams = (await sess.exec(
                select(persistent.ReadCapWAL).where(
                    persistent.ReadCapWAL.read_cap == stub_read_cap,
                )
            )).all()
            assert len(substreams) == 1
            rcw = substreams[0]
            assert rcw.substream_total_chunks == 7
            assert rcw.next_index == stub_read_cap[-104:]
            peers = (await sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.read_cap_id == rcw.id,
                )
            )).all()
            assert len(peers) == 1
            assert peers[0].name.startswith(network._SUBSTREAM_NAME_PREFIX)
            assert peers[0].active is True
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "started"
        _, started_rcw, conv_id, total, parent_name = event
        assert started_rcw == rcw.id
        assert conv_id == setup["conversation_id"]
        assert total == 7
        assert parent_name == "parent_alice"
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_legacy_i_chunk_creates_substream_without_total(
        self, fake_thinclient,
    ):
        """Receive side, legacy form: a plain 136-byte I-chunk
        (no total prefix) still spawns the substream, but the ReadCapWAL's
        total stays None and the ``started`` event's total is None so the
        Transfers panel renders indeterminate progress."""
        stub_read_cap = b"\xdd" * 136
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=b"I" + stub_read_cap,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            substreams = (await sess.exec(
                select(persistent.ReadCapWAL).where(
                    persistent.ReadCapWAL.read_cap == stub_read_cap,
                )
            )).all()
            assert len(substreams) == 1
            assert substreams[0].substream_total_chunks is None
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "started"
        assert event[3] is None
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_substream_piece_read_fires_piece_event(
        self, fake_thinclient,
    ):
        """Reading a C-chunk on a substream peer queues a
        single ``piece`` event carrying the accumulated ReceivedPiece count
        and effective payload bytes for that substream (matching the Transfers
        panel's n/total and rate)."""
        setup = await _set_up_read_flow(
            fake_thinclient, peer_name=":substream:2:abc", plaintext=b"Cchunk",
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "piece"
        assert event[1] == setup["bacap_stream"]
        assert event[2] == 1  # the C-chunk just stored counts as one piece
        assert event[3] == 5  # b"chunk": payload after the type byte
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extended", [False, True])
    async def test_substream_terminal_f_fires_completed_event(
        self, fake_thinclient: FakeThinClient, extended: bool,
    ) -> None:
        """Assembling the substream's terminal F (through a
        parent peer that resolves from the substream name) retires the
        substream and queues a single ``completed`` event so the Transfers
        panel drops the row."""
        # Parent conversation + peer first (auto pk=2), so the substream
        # name ":substream:2:abc" resolves during dispatch.
        async with persistent.asession() as sess:
            wcw_id = uuid.uuid4()
            wcw = persistent.WriteCapWAL(
                id=wcw_id, write_cap=b"\xab" * 168, next_index=b"\x00" * 104,
            )
            rcw = persistent.ReadCapWAL(
                id=wcw_id, write_cap_id=wcw_id,
                read_cap=b"\xac" * 136, next_index=b"\x00" * 104,
            )
            sess.add_all((wcw, rcw))
            await sess.flush()
            parent_peer = persistent.ConversationPeer(
                name="carol",
                read_cap_id=wcw_id,
                active=True,
            )
            sess.add(parent_peer)
            await sess.flush()
            parent_id = parent_peer.id
            await sess.commit()
            parent_conv = persistent.Conversation(
                name="carol-conv", own_peer_id=parent_id,
                write_cap=wcw_id,
            )
            sess.add(parent_conv)
            await sess.commit()
            await sess.refresh(parent_conv)
            link = persistent.ConversationPeerLink(
                conversation_peer_id=parent_id,
                conversation_id=parent_conv.id,
            )
            sess.add(link)
            await sess.commit()
            assert parent_id == 1

        setup = await _set_up_read_flow(
            fake_thinclient,
            peer_name=f":substream:{parent_id}:abc",
            plaintext=_make_F_payload("finalised"),
        )
        async with persistent.asession() as sess:
            release = setup["read_cap"]
            if extended:
                release = struct.pack(">I", 1) + release
            sess.add(persistent.ReceivedPiece(
                read_cap=wcw_id, bacap_index=bytes(8),
                chunk_type=b"I", chunk=release,
            ))
            await sess.commit()
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "piece"
        assert event[1] == setup["bacap_stream"]
        assert event[2] == 1
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "completed"
        assert event[1] == setup["bacap_stream"]
        assert network.substream_progress_queue.empty()

        async with persistent.asession() as sess:
            assert await sess.get(
                persistent.ReceivedPiece, (wcw_id, bytes(8)),
            ) is None

    @pytest.mark.asyncio
    async def test_invalid_prefix_deactivates_peer(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Xunknown")
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        # Peer marked inactive, MW removed, no ConversationLog appended.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            peer = await sess.get(persistent.ConversationPeer, setup["peer_id"])
            assert peer.active is False
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert log == []

    @pytest.mark.asyncio
    async def test_stale_ack_branch_deletes_mw_without_advancing(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Fhi")
        # Pretend the index has already been advanced past mw.next_message_index.
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            # Set RCW.next_index = mw.next_message_index so idx_old == idx_new
            # which trips the regression-guard branch.
            rcw.next_index = setup["rcr"].next_message_box_index
            sess.add(rcw)
            await sess.commit()
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert log == []  # no append on stale ACK

    @pytest.mark.asyncio
    async def test_bacap_decryption_failure_gives_up(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", BACAPDecryptionFailedError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_mkem_decryption_failure_gives_up(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", MKEMDecryptionFailedError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_cancelled_resend_gives_up(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", StartResendingCancelledError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_os_error_gives_up(self, fake_thinclient):
        """An OS-level send failure (``[Errno 9] Bad file descriptor`` after a
        daemon reconnect closes the socket) is transient, not fatal: the box
        must be left for a re-cast and the stream released instead of stranded
        in draining_right_now (which silently starves every later box)."""
        payload = _make_F_payload("os error then retry")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", OSError(9, "Bad file descriptor"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["first_message_index"]
        assert setup["bacap_stream"] not in draining
        # The injected error popped on the first call, so the same box is
        # re-cast (un-injected) and completes normally.
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1 and log[0].payload == payload
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_unhandled_read_exception_leaves_mw_for_retry(self, fake_thinclient):
        """An exception not in the give-up list propagates (the drain loop's
        done-callback is what releases the stream in that case), but it must
        never advance the read cursor or delete the box."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", RuntimeError("boom"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        with pytest.raises(RuntimeError):
            await network.drain_mixwal_read_single(
                connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
                mw=mw, draining_right_now={setup["bacap_stream"]},
            )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["first_message_index"]
            assert (await sess.exec(select(persistent.ConversationLog))).all() == []

    @pytest.mark.asyncio
    async def test_database_failure_reschedules(self, fake_thinclient):
        """A transient replica database failure must back off and retry: the
        MixWAL row survives (the stream is not advanced) and the stream is
        released from draining_right_now so it can be picked up again. This is
        the only replica error code that reaches a read; ReplicationFailed and
        InternalError are served by the courier as ACKs and never surface."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", DatabaseFailureError("database failure"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_courier_error_reschedules(self, fake_thinclient):
        """A courier-side rejection is distinct from a replica error and must
        not wedge the stream: it leaves the MixWAL for retry and releases the
        stream from draining_right_now. Guards against the former collision
        where a courier error arrived as a replica database failure."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierError("courier rejected envelope"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert setup["bacap_stream"] not in draining

    @pytest.mark.parametrize("benign", [
        BoxIDNotFoundError("box ID not found"),
        TombstoneError("tombstone"),
    ])
    @pytest.mark.asyncio
    async def test_benign_replica_outcome_does_not_wedge(self, fake_thinclient, benign):
        """A benign replica read outcome (no data yet, or a tombstone) must not
        be treated as a failure: it must not crash, must leave the MixWAL for a
        later retry, and must release the stream from draining_right_now so it
        is not stranded (an uncaught one would wedge the stream)."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", benign,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert setup["bacap_stream"] not in draining

    @pytest.mark.parametrize("benign", [
        BoxIDNotFoundError("box ID not found"),
        TombstoneError("tombstone"),
    ])
    @pytest.mark.asyncio
    async def test_substream_tombstone_is_terminal_but_not_found_retries(
        self, fake_thinclient: FakeThinClient,
        monkeypatch: pytest.MonkeyPatch, benign: Exception,
    ) -> None:
        """A missing box retries at the same index; a tombstone retires
        the transfer and publishes its failure after the commit."""
        setup = await _set_up_read_flow(
            fake_thinclient, peer_name=":substream:2:abc",
        )
        # Record the no_retry flag (the fake's call_log omits it).
        recorded = {}
        orig = fake_thinclient.start_resending_encrypted_message

        async def recording_resend(*args, **kwargs):
            recorded["no_retry_on_box_id_not_found"] = kwargs.get(
                "no_retry_on_box_id_not_found",
            )
            return await orig(*args, **kwargs)

        monkeypatch.setattr(
            fake_thinclient, "start_resending_encrypted_message",
            recording_resend,
        )
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", benign,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        assert recorded["no_retry_on_box_id_not_found"] is True
        async with persistent.asession() as sess:
            # Only a tombstone retires the transfer on its first attempt.
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is (not isinstance(benign, TombstoneError))
            remaining = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert (remaining is None) is isinstance(benign, TombstoneError)
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_normal_peer_not_found_is_benign(self, fake_thinclient, monkeypatch):
        """A normal-conversation BoxIDNotFound is failed fast too: every read
        is cast with no_retry_on_box_id_not_found=True, so the daemon reports
        'no further data yet' immediately and the local polling delay re-casts
        the read. Unlike a substream, this must NOT deactivate the peer or
        drop its MixWAL."""
        setup = await _set_up_read_flow(fake_thinclient, peer_name="self")
        recorded = {}
        orig = fake_thinclient.start_resending_encrypted_message

        async def recording_resend(*args, **kwargs):
            recorded["no_retry_on_box_id_not_found"] = kwargs.get(
                "no_retry_on_box_id_not_found",
            )
            return await orig(*args, **kwargs)

        monkeypatch.setattr(
            fake_thinclient, "start_resending_encrypted_message",
            recording_resend,
        )
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            BoxIDNotFoundError("box ID not found"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        assert recorded["no_retry_on_box_id_not_found"] is True
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_substream_unprocessable_chunk_fires_failed_event(
        self, fake_thinclient, monkeypatch,
    ):
        """A substream peer that hits an unprocessable exception during
        response processing should deactivate the peer, advance the cursor,
        delete the MixWAL, and fire a 'failed' event."""
        from katzenqt import conversation_handlers
        
        # Set up a parent peer so the substream name resolves correctly.
        # Substream name format is ":substream:{parent_id}:{nonce}".
        async with persistent.asession() as sess:
            wcw_id = uuid.uuid4()
            wcw = persistent.WriteCapWAL(
                id=wcw_id, write_cap=b"\xab" * 168, next_index=b"\x00" * 104,
            )
            rcw = persistent.ReadCapWAL(
                id=wcw_id, write_cap_id=wcw_id,
                read_cap=b"\xac" * 136, next_index=b"\x00" * 104,
            )
            sess.add_all((wcw, rcw))
            await sess.flush()
            parent_peer = persistent.ConversationPeer(
                name="carol",
                read_cap_id=wcw_id,
                active=True,
            )
            sess.add(parent_peer)
            await sess.flush()
            parent_id = parent_peer.id
            await sess.commit()
            parent_conv = persistent.Conversation(
                name="carol-conv", own_peer_id=parent_id,
                write_cap=wcw_id,
            )
            sess.add(parent_conv)
            await sess.commit()
            await sess.refresh(parent_conv)
            link = persistent.ConversationPeerLink(
                conversation_peer_id=parent_id,
                conversation_id=parent_conv.id,
            )
            sess.add(link)
            await sess.commit()
            assert parent_id == 1
        
        # Use an F-chunk (terminal chunk) to exercise the dispatch path.
        setup = await _set_up_read_flow(
            fake_thinclient,
            peer_name=f":substream:{parent_id}:abc",
            plaintext=_make_F_payload("test file"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        
        # Monkeypatch conversation_handlers.dispatch to raise an exception.
        # This simulates a processing error (e.g., CBOR decode failure, CRDT
        # error) that gets caught by the generic exception handler.
        async def failing_dispatch(sess, peer, gcm, full_payload):
            raise ValueError("malformed chunk data")
        
        monkeypatch.setattr(conversation_handlers, "dispatch", failing_dispatch)
        
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        
        # The dispatch fails, so the "piece" event is never fired (the
        # ReceivedPiece add is rolled back). Only the "failed" event appears.
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "failed"
        assert event[1] == str(setup["bacap_stream"])
        assert "ValueError: malformed chunk data" in event[2]
        assert network.substream_progress_queue.empty()
        
        # Verify peer deactivated
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is False
        # Verify stream released from draining
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_normal_peer_unprocessable_chunk_advances_without_failing(
        self, fake_thinclient, monkeypatch,
    ):
        """A normal (non-substream) peer that hits an unprocessable exception
        during processing should advance past it without deactivating the peer
        or firing a 'failed' event."""
        from katzenqt import conversation_handlers
        
        setup = await _set_up_read_flow(
            fake_thinclient, peer_name="alice",
            plaintext=_make_F_payload("test"),  # F-chunk triggers dispatch
        )
        
        async def failing_dispatch(sess, peer, gcm, full_payload):
            raise ValueError("bad data")
        
        monkeypatch.setattr(conversation_handlers, "dispatch", failing_dispatch)
        
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        
        # Verify peer still active
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
        # Verify no 'failed' event fired
        assert network.substream_progress_queue.empty()
        # Verify stream released from draining
        assert setup["bacap_stream"] not in draining

    @pytest.mark.asyncio
    async def test_lost_encrypt_read_is_recovered_after_reconnect(
        self, fake_thinclient, monkeypatch,
    ):
        """A reconnect mid-fresh-`encrypt_read` (nothing dispatched) must
        release the stream for a fresh re-encrypt on the reconnected
        client."""
        payload = _make_F_payload("hang on encrypt then reconnect")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        # Hold the drain's re-encrypt so it orphans.
        held = asyncio.Event()
        reencrypt_started = asyncio.Event()
        recorded = {"n": 0}

        async def hanging_encrypt_read(read_cap, message_box_index):
            recorded["n"] += 1
            reencrypt_started.set()
            await held.wait()  # never set in this test
            raise AssertionError("unreachable")

        monkeypatch.setattr(
            fake_thinclient, "encrypt_read", hanging_encrypt_read,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}

        async def simulate_reconnect():
            # Fire the reconnect only once the encrypt_read is in flight
            # (marker-swap caveat as above).
            await asyncio.wait_for(reencrypt_started.wait(), timeout=5.0)
            await network.on_connection_status({"is_connected": False, "err": None})
            await network.on_connection_status({"is_connected": True, "err": None})

        reconnector = asyncio.create_task(simulate_reconnect())
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
            read_watchdog_s=60.0,
            reconnect_grace_s=0.05,
        )
        await reconnector
        assert recorded["n"] == 1
        # No daemon call to cancel; the stream is released for re-cast.
        assert fake_thinclient.call_count("cancel_resending_encrypted_message") == 0
        assert setup["bacap_stream"] not in draining
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["first_message_index"]
            assert (await sess.exec(select(persistent.ConversationLog))).all() == []

    @pytest.mark.asyncio
    async def test_courier_invalid_epoch_reschedules_without_reminting(self, fake_thinclient):
        """A stale-replica-epoch rejection is permanent for the stored blob,
        but the read path never resends that blob: it re-encrypts a fresh
        envelope at the top of every pass. So the drain must simply keep the
        row, release the stream and signal the scheduler -- and must NOT
        spend a second encrypt_read re-minting the row's dead envelope."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("courier rejected envelope: replica epoch outside tolerance window"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        getattr(network, "__mixwal_updated").clear()
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash == setup["rcr"].envelope_hash
            assert row.encrypted_payload == setup["rcr"].message_ciphertext
            assert row.current_message_index == setup["first_message_index"]
        # One in _set_up_read_flow, one at the top of the drain pass.
        assert fake_thinclient.call_count("encrypt_read") == 2
        last = fake_thinclient.last_call("encrypt_read")
        assert last["read_cap"] == setup["read_cap"]
        assert last["message_box_index"] == setup["first_message_index"]
        assert setup["bacap_stream"] not in draining
        assert getattr(network, "__mixwal_updated").is_set()

    @pytest.mark.asyncio
    async def test_generic_courier_error_still_reschedules(self, fake_thinclient):
        """Other courier rejections (malformed envelope, cache corruption)
        leave the MixWAL untouched for retry and release the stream, exactly
        like a stale epoch does."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", CourierError("boom"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        getattr(network, "__mixwal_updated").clear()
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash == setup["rcr"].envelope_hash
        # One in _set_up_read_flow, one at the top of the drain pass.
        assert fake_thinclient.call_count("encrypt_read") == 2
        assert setup["bacap_stream"] not in draining
        assert getattr(network, "__mixwal_updated").is_set()

    @pytest.mark.asyncio
    async def test_courier_invalid_epoch_then_drain_succeeds(self, fake_thinclient):
        """Full recovery: one stale-epoch rejection, then the next pass's
        freshly encrypted envelope drains normally and the stream advances."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("stale"),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            # Deterministic derivation: re-encrypting at the same current
            # index yields the same next index as the original envelope.
            assert rcw.next_index == setup["rcr"].next_message_box_index

    @pytest.mark.asyncio
    async def test_read_setup_failure_releases_stream(self, fake_thinclient):
        """If the pass's own encrypt_read fails (daemon offline, no PKI doc),
        nothing is dispatched: the row must be kept as-is, the stream
        released and the scheduler signalled, with no exception escaping."""
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error("encrypt_read", ThinClientOfflineError())
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        getattr(network, "__mixwal_updated").clear()
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        async with persistent.asession() as sess:
            row = await sess.get(persistent.MixWAL, setup["mw_id"])
            assert row is not None
            assert row.envelope_hash == setup["rcr"].envelope_hash
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 0
        assert setup["bacap_stream"] not in draining
        assert getattr(network, "__mixwal_updated").is_set()



class TestPauseResumePeerReads:
    """Per-peer pause/resume. A user-initiated pause on a
    peer must cancel the in-flight read ARQ (so the daemon stops
    retransmitting), delete the is_read MixWAL row (so the drain sweep
    cannot re-cast it), and pause the read cap so readables_to_mixwal
    never re-arms it. Resume must clear the pause and poke the re-arm event."""

    @pytest.mark.asyncio
    async def test_pause_cancels_inflight_read(self, fake_thinclient, monkeypatch):
        setup = await _set_up_read_flow(fake_thinclient, peer_name=":substream:2:abc")
        # Hold the drain inside the reply race so there is a genuine
        # in-flight ARQ to cancel (rcr is set, so the CancelledError
        # handler must cancel it at the daemon).
        replay_arrived = asyncio.Event()
        stuck_forever = asyncio.Event()

        async def stuck_resend(*args, **kwargs):
            replay_arrived.set()
            await stuck_forever.wait()  # never set in this test

        monkeypatch.setattr(
            fake_thinclient, "start_resending_encrypted_message", stuck_resend,
        )
        cancels = []
        orig_cancel = fake_thinclient.cancel_resending_encrypted_message

        async def recording_cancel(envelope_hash):
            cancels.append(envelope_hash)
            return await orig_cancel(envelope_hash)

        monkeypatch.setattr(
            fake_thinclient, "cancel_resending_encrypted_message", recording_cancel,
        )
        # The drain re-encrypts fresh (network.py:670), and the fake's
        # encrypt_read mints a NEW envelope_hash each call, so capture the
        # drain's envelope rather than comparing against setup["rcr"].
        fresh_encrypts = []
        orig_encrypt = fake_thinclient.encrypt_read

        async def recording_encrypt(*args, **kwargs):
            r = await orig_encrypt(*args, **kwargs)
            fresh_encrypts.append(r.envelope_hash)
            return r

        monkeypatch.setattr(
            fake_thinclient, "encrypt_read", recording_encrypt,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        read_task = asyncio.create_task(network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        ))
        # Mirror drain_mixwal2's registry entry AND its done-callback so a
        # pause can reach the task and the callback releases the stream.
        network._inflight_reads[setup["bacap_stream"]] = read_task

        def _on_read_done(*_args):
            network._inflight_reads.pop(setup["bacap_stream"], None)
            draining.discard(setup["bacap_stream"])

        read_task.add_done_callback(_on_read_done)
        await asyncio.wait_for(replay_arrived.wait(), timeout=5.0)

        await network.pause_peer_reads(bacap_stream=setup["bacap_stream"])

        assert read_task.cancelled()
        assert cancels == fresh_encrypts and len(fresh_encrypts) == 1
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.read_paused is True
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
        # The done-callback released the stream from draining_right_now.
        assert setup["bacap_stream"] not in draining
        await asyncio.sleep(0)  # let the done-callback run
        assert network._inflight_reads.get(setup["bacap_stream"]) is None

    @pytest.mark.asyncio
    async def test_pause_with_no_inflight_read_keeps_membership(
        self, fake_thinclient: FakeThinClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pause on a stream with no registered in-flight task (the read
        completed on its own, or we are pausing before the loop ever armed
        it) must still drop the MW row and pause the read cap without
        changing membership."""
        setup = await _set_up_read_flow(fake_thinclient, peer_name=":substream:2:abc")
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

        await network.pause_peer_reads(bacap_stream=setup["bacap_stream"])

        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.read_paused is True
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
        # The pause announces itself to the Transfers panel.
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "paused"
        assert event[1] == setup["bacap_stream"]
        # Resume must run on a paused stream even though the drain loop
        # re-arms it (fresh MW) only when a connection is present.
        await network.resume_peer_reads(bacap_stream=setup["bacap_stream"])
        async with persistent.asession() as sess:
            cp = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).one()
            assert cp.active is True
        event = network.substream_progress_queue.get_nowait()
        assert event[0] == "resumed"
        assert event[1] == setup["bacap_stream"]

    @pytest.mark.asyncio
    async def test_resume_rearms_read_from_saved_index(self, fake_thinclient):
        """After a pause deletes the MW row but keeps the ReadCapWAL
        next_index cursor, resume + a readables_to_mixwal pass must arm a
        FRESH is_read MW from the saved index (the exact message_index the
        paused read was on), not restart from the beginning."""
        payload = _make_F_payload("resume re-arms from saved index")
        setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
        await network.pause_peer_reads(bacap_stream=setup["bacap_stream"])
        await network.resume_peer_reads(bacap_stream=setup["bacap_stream"])

        # Prerequisites for readables_to_mixwal's loop to enter its body.
        await network.on_connection_status({"is_connected": True, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "readables_to_mixwal_event").set()

        async def rearmed():
            async with persistent.asession() as sess:
                rows = (await sess.exec(select(persistent.MixWAL).where(
                    persistent.MixWAL.is_read,
                ))).all()
                return len(rows) == 1 and rows[0].bacap_stream == setup["bacap_stream"]

        await _run_loop_until(
            network.readables_to_mixwal(fake_thinclient),
            rearmed,
            timeout=5.0,
        )
        async with persistent.asession() as sess:
            rows = (await sess.exec(select(persistent.MixWAL).where(
                persistent.MixWAL.is_read,
            ))).all()
        assert len(rows) == 1
        assert rows[0].bacap_stream == setup["bacap_stream"]
        assert rows[0].current_message_index == setup["first_message_index"]

    @pytest.mark.asyncio
    async def test_duplicate_readable_peer_arms_stream_once(self, fake_thinclient):
        """Two active ConversationPeer rows aliasing ONE ReadCapWAL (a stale
        duplicate-identity transient) must arm the stream exactly once per
        pass. Without the dedupe, a pass adds two is_read MixWAL rows with
        the same bacap_stream and its single commit raises IntegrityError
        (UNIQUE constraint failed: mixwal.bacap_stream), killing
        readables_to_mixwal -- the session's only read-arming task -- and
        wedging every later read (observed as a send-file timeout)."""
        setup = await _insert_write_setup(fake_thinclient)
        fake_thinclient.pre_store(
            write_cap=setup["write_cap"],
            message_box_index=setup["first_message_index"],
            plaintext=_make_F_payload("duplicate peer arms stream once"),
        )
        async with persistent.asession() as sess:
            dup_peer = persistent.ConversationPeer(
                name="self:duplicate",
                read_cap_id=setup["bacap_stream"],
                active=True,
            )
            sess.add(dup_peer)
            await sess.commit()

        await network.on_connection_status({"is_connected": True, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "readables_to_mixwal_event").set()

        async def armed_once():
            async with persistent.asession() as sess:
                rows = (await sess.exec(select(persistent.MixWAL).where(
                    persistent.MixWAL.is_read,
                ))).all()
                return len(rows) == 1 and rows[0].bacap_stream == setup["bacap_stream"]

        await _run_loop_until(
            network.readables_to_mixwal(fake_thinclient),
            armed_once,
            timeout=5.0,
        )
        async with persistent.asession() as sess:
            rows = (await sess.exec(select(persistent.MixWAL).where(
                persistent.MixWAL.is_read,
            ))).all()
        assert len(rows) == 1
        assert rows[0].bacap_stream == setup["bacap_stream"]
        async with persistent.asession() as sess:
            peers = (await sess.exec(select(persistent.ConversationPeer).where(
                persistent.ConversationPeer.read_cap_id == setup["bacap_stream"],
            ))).all()
        assert len(peers) == 2  # both identities remain; only one stream armed


# ---------------------------------------------------------------------------
# start_resending (PlaintextWAL -> MixWAL)
# ---------------------------------------------------------------------------


class TestStartResending:
    @pytest.mark.asyncio
    async def test_creates_mixwal_matching_encrypt_write_reply(self, fake_thinclient):
        setup = await _insert_write_setup(fake_thinclient)
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Fhello",
            )
            sess.add(pwal)
            await sess.commit()
            await sess.refresh(pwal)
        await network.start_resending(fake_thinclient, pwal)
        # A MixWAL row should now exist for this stream.
        async with persistent.asession() as sess:
            mws = (await sess.exec(
                select(persistent.MixWAL).where(
                    persistent.MixWAL.bacap_stream == setup["bacap_stream"]
                )
            )).all()
            assert len(mws) == 1
            mw = mws[0]
            assert mw.is_read is False
            assert mw.plaintextwal == pwal.id
        # Compare against the fake's last encrypt_write call to confirm
        # the wire shape matches.
        last_encrypt = fake_thinclient.last_call("encrypt_write")
        assert last_encrypt["plaintext"] == b"Fhello"
        assert last_encrypt["write_cap"] == setup["write_cap"]


# ---------------------------------------------------------------------------
# provision_read_caps
# ---------------------------------------------------------------------------


class TestProvisionReadCaps:
    @pytest.mark.asyncio
    async def test_populates_blank_caps(self, fake_thinclient):
        bacap_stream = uuid.uuid4()
        async with persistent.asession() as sess:
            wcw = persistent.WriteCapWAL(id=bacap_stream)
            rcw = persistent.ReadCapWAL(id=bacap_stream, write_cap_id=bacap_stream)
            sess.add_all([wcw, rcw])
            await sess.commit()
        # Run provision_read_caps inline (not via asyncio.create_task) so
        # coverage's tracer follows the body. A separate quitter task sets
        # __should_quit so the function returns on its next while-check.
        async def quitter():
            for _ in range(20):
                await asyncio.sleep(0)
                async with persistent.asession() as sess:
                    rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
                    if rcw.read_cap is not None:
                        break
            network.shutdown()
        asyncio.create_task(quitter())
        await asyncio.wait_for(
            network.provision_read_caps(fake_thinclient), timeout=5.0,
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
            wcw = await sess.get(persistent.WriteCapWAL, bacap_stream)
            assert rcw.read_cap is not None
            assert wcw.write_cap is not None
            assert wcw.write_cap[32:] == rcw.read_cap

    @pytest.mark.asyncio
    async def test_already_populated_write_cap_takes_warning_branch(
        self, fake_thinclient,
    ):
        bacap_stream = uuid.uuid4()
        # WriteCapWAL has a write_cap but ReadCapWAL.read_cap is still NULL.
        async with persistent.asession() as sess:
            wcw = persistent.WriteCapWAL(
                id=bacap_stream,
                write_cap=b"\x00" * 168,
                next_index=b"\x00" * 104,
            )
            rcw = persistent.ReadCapWAL(id=bacap_stream, write_cap_id=bacap_stream)
            sess.add_all([wcw, rcw])
            await sess.commit()
        task = asyncio.create_task(network.provision_read_caps(fake_thinclient))
        try:
            # Give the loop a couple of iterations to encounter the row.
            for _ in range(50):
                await asyncio.sleep(0)
            async with persistent.asession() as sess:
                rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
            # The function explicitly warns and continues — read_cap stays None.
            assert rcw.read_cap is None
            # new_keypair must NOT have been called.
            assert fake_thinclient.call_count("new_keypair") == 0
        finally:
            # Clean exit: set __should_quit so the while-loop sees it and
            # falls out at the top of its next iteration. Avoids cancelling
            # mid-session, which would leave a SQLite lock dangling and the
            # next test's DROP TABLE waiting.
            network.shutdown()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_new_keypair_failure_does_not_break_loop(self, fake_thinclient):
        bacap_stream = uuid.uuid4()
        async with persistent.asession() as sess:
            wcw = persistent.WriteCapWAL(id=bacap_stream)
            rcw = persistent.ReadCapWAL(id=bacap_stream, write_cap_id=bacap_stream)
            sess.add_all([wcw, rcw])
            await sess.commit()
        # First call raises, second succeeds.
        fake_thinclient.inject_error("new_keypair", RuntimeError("transient"))
        task = asyncio.create_task(network.provision_read_caps(fake_thinclient))
        try:
            for _ in range(400):
                await asyncio.sleep(0)
                async with persistent.asession() as sess:
                    rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
                    if rcw.read_cap is not None:
                        break
            assert rcw.read_cap is not None
            assert fake_thinclient.call_count("new_keypair") >= 2
        finally:
            # Clean exit: set __should_quit so the while-loop sees it and
            # falls out at the top of its next iteration. Avoids cancelling
            # mid-session, which would leave a SQLite lock dangling and the
            # next test's DROP TABLE waiting.
            network.shutdown()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# drain_mixwal2 (dispatch loop)
# ---------------------------------------------------------------------------


# The genuine asyncio.sleep, captured before the autouse fast_asyncio_sleep
# fixture rebinds asyncio.sleep to an instant stub. The poll loop below needs
# a real sleep, not the stub: a zero-delay busy-spin never lets the event loop
# idle in epoll, so it starves the aiosqlite worker thread of GIL time and the
# detached DB work in start_resending crawls, racing the deadline (flaky).
_REAL_SLEEP = asyncio.sleep


async def _run_loop_until(loop_coro, condition_callable, *, timeout: float = 5.0):
    """Run a long-running coroutine and shut it down once
    `condition_callable()` returns truthy or the wall-clock deadline
    elapses.

    The wait is bounded by `asyncio.get_event_loop().time()` so the
    autouse fast_asyncio_sleep monkeypatch can't accidentally race the
    loop body. condition_callable may be sync or async.
    """
    is_async = asyncio.iscoroutinefunction(condition_callable)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    loop_task = asyncio.create_task(loop_coro)
    try:
        while True:
            await _REAL_SLEEP(0.002)
            result = await condition_callable() if is_async else condition_callable()
            if result:
                break
            if loop.time() >= deadline:
                break
            if loop_task.done():
                break
    finally:
        network.shutdown()
        try:
            await asyncio.wait_for(loop_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            loop_task.cancel()


class TestDrainMixwal2:
    @pytest.mark.real_sleeps
    @pytest.mark.asyncio
    async def test_dispatches_write_mixwal(self, fake_thinclient):
        """drain_mixwal2 must dispatch a write MixWAL through to the
        thin-client. Opt out of the fast-sleep monkeypatch so the
        dispatched task gets real wall-time to complete its async DB
        commits inside SentLog.mark_sent."""
        setup = await _set_up_write_flow(fake_thinclient)
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        def dispatch_happened():
            return fake_thinclient.call_count(
                "start_resending_encrypted_message"
            ) >= 1

        loop_task = asyncio.create_task(network.drain_mixwal2(fake_thinclient))
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if dispatch_happened():
                    break
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()
        assert dispatch_happened()
        last = fake_thinclient.last_call("start_resending_encrypted_message")
        assert last["envelope_hash"] == setup["wcr"].envelope_hash

    @pytest.mark.asyncio
    async def test_dispatches_read_mixwal(self, fake_thinclient):
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=_make_F_payload("hi"),
        )
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        async def mw_drained():
            async with persistent.asession() as sess:
                return await sess.get(persistent.MixWAL, setup["mw_id"]) is None

        await _run_loop_until(
            network.drain_mixwal2(fake_thinclient), mw_drained, timeout=5.0,
        )
        assert await mw_drained()
        # ConversationLog should have the message appended.
        async with persistent.asession() as sess:
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1

    @pytest.mark.asyncio
    async def test_introduction_adds_peer_and_emits_peer_added(self, fake_thinclient):
        """Receiving an INTRODUCTION for a genuinely new member must both
        persist the peer (so their stream gets read) and put a
        (conversation_id, name) on ``peer_added_queue`` so the GUI can show
        the newcomer on every live client without a restart."""
        announced = models.GroupChatPleaseAdd(
            display_name="carol", read_cap=b"\xaa" * 136,
        )
        intro = models.GroupChatMessage(
            version=0, membership_hash=b"X" * 32,
            msg_type=models.GroupChatTypeEnum.INTRODUCTION,
            introduction=announced,
        )
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=b"F" + intro.to_cbor(),
        )
        # Drain the queue of anything a previous test left behind (the
        # module-level queue is shared across the suite).
        queue = network.peer_added_queue
        while not queue.empty():
            queue.get_nowait()
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            peers = (await sess.exec(select(persistent.ConversationPeer))).all()
            names = {p.name for p in peers}
            assert "carol" in names
            for peer in peers:
                if peer.name == "carol":
                    assert peer.active is True
        assert not queue.empty()
        conv_id, name = queue.get_nowait()
        assert (conv_id, name) == (setup["conversation_id"], "carol")

    @pytest.mark.asyncio
    async def test_introduction_own_announcement_does_not_emit(self, fake_thinclient):
        """An INTRODUCTION about ourselves (own salt-mutated read cap) is
        stored as history but must not add a peer or emit a peer_added event."""
        setup = await _set_up_read_flow(fake_thinclient)
        own_cap = setup["write_cap"][32:]
        intro = models.GroupChatMessage(
            version=0, membership_hash=b"X" * 32,
            msg_type=models.GroupChatTypeEnum.INTRODUCTION,
            introduction=models.GroupChatPleaseAdd(
                display_name="self", read_cap=own_cap,
            ),
        )
        queue = network.peer_added_queue
        while not queue.empty():
            queue.get_nowait()
        # Place the announcement in the box we are about to read.
        fake_thinclient.pre_store(
            write_cap=setup["write_cap"],
            message_box_index=setup["first_message_index"],
            plaintext=b"F" + intro.to_cbor(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            peers = (await sess.exec(select(persistent.ConversationPeer))).all()
            assert [p.name for p in peers] == ["self"]
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_malformed_read_cap_does_not_kill_the_whole_drain_loop(
        self, fake_thinclient,
    ):
        """A single corrupted ReadCapWAL row (wrong-length read_cap) must
        fail only its own stream (logged CRITICAL by drain_mixwal's
        wrapper); every other stream (read or write) keeps draining as
        normal."""
        healthy = await _set_up_read_flow(
            fake_thinclient, plaintext=_make_F_payload("hi"),
        )
        broken = await _set_up_read_flow(
            fake_thinclient, seed=b"\x44" * 32, conv_name="demo2",
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, broken["bacap_stream"])
            rcw.read_cap = rcw.read_cap[:-1]
            sess.add(rcw)
            await sess.commit()

        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        async def healthy_drained():
            async with persistent.asession() as sess:
                return await sess.get(persistent.MixWAL, healthy["mw_id"]) is None

        loop_task = asyncio.create_task(network.drain_mixwal2(fake_thinclient))
        try:
            for _ in range(500):
                await asyncio.sleep(0.02)
                if await healthy_drained() or loop_task.done():
                    break
            assert not loop_task.done(), (
                f"drain_mixwal2 crashed instead of skipping the malformed "
                f"stream: {loop_task.exception() if loop_task.done() else None}"
            )
            assert await healthy_drained()
            # The malformed one is left alone (not silently deleted), just
            # released so it doesn't strand draining_right_now.
            async with persistent.asession() as sess:
                assert await sess.get(persistent.MixWAL, broken["mw_id"]) is not None
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_read_stale_epoch_resends_via_scheduler(self, fake_thinclient):
        """End of the recovery loop: a stale-epoch rejection releases the
        stream, the scheduler is signalled, and the next pass's freshly
        encrypted envelope drains. The 5 s deadline sits under
        drain_mixwal2's 15 s poll, so this fails if the release-then-signal
        ordering regresses."""
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=_make_F_payload("hi"),
        )
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            CourierInvalidEpochError("stale"),
        )
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        async def mw_drained():
            async with persistent.asession() as sess:
                return await sess.get(persistent.MixWAL, setup["mw_id"]) is None

        await _run_loop_until(
            network.drain_mixwal2(fake_thinclient), mw_drained, timeout=5.0,
        )
        assert await mw_drained()
        assert fake_thinclient.call_count("start_resending_encrypted_message") >= 2
        async with persistent.asession() as sess:
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1

# ---------------------------------------------------------------------------
# readables_to_mixwal
# ---------------------------------------------------------------------------


class TestReadablesToMixwal:
    @pytest.mark.asyncio
    async def test_active_peer_becomes_mixwal_entry(self, fake_thinclient):
        # An active ConversationPeer with a provisioned RCW but no MixWAL
        # row yet should be picked up and a read-MixWAL row inserted.
        setup = await _insert_write_setup(fake_thinclient)
        # Prerequisites for the loop to enter the body.
        await network.on_connection_status({"is_connected": True, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "readables_to_mixwal_event").set()

        async def mixwal_inserted():
            async with persistent.asession() as sess:
                mws = (await sess.exec(
                    select(persistent.MixWAL).where(
                        persistent.MixWAL.bacap_stream == setup["bacap_stream"]
                    )
                )).all()
                return len(mws) >= 1

        await _run_loop_until(
            network.readables_to_mixwal(fake_thinclient),
            mixwal_inserted,
            timeout=5.0,
        )
        assert await mixwal_inserted()
        # The fake should have seen an encrypt_read call.
        assert fake_thinclient.call_count("encrypt_read") >= 1

    @pytest.mark.asyncio
    async def test_inactive_peer_is_not_polled(self, fake_thinclient):
        setup = await _insert_write_setup(fake_thinclient)
        # Mark the peer inactive.
        async with persistent.asession() as sess:
            peer = await sess.get(persistent.ConversationPeer, setup["peer_id"])
            peer.active = False
            sess.add(peer)
            await sess.commit()
        await network.on_connection_status({"is_connected": True, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "readables_to_mixwal_event").set()

        async def quitter():
            for _ in range(40):
                await asyncio.sleep(0)
            network.shutdown()

        asyncio.create_task(quitter())
        try:
            await asyncio.wait_for(
                network.readables_to_mixwal(fake_thinclient), timeout=3.0,
            )
        except asyncio.TimeoutError:
            pass
        # encrypt_read must not have been called for an inactive peer.
        assert fake_thinclient.call_count("encrypt_read") == 0


# ---------------------------------------------------------------------------
# send_resendable_plaintexts
# ---------------------------------------------------------------------------


class TestSendResendablePlaintexts:
    @pytest.mark.asyncio
    async def test_plaintextwal_becomes_mixwal(self, fake_thinclient):
        setup = await _insert_write_setup(fake_thinclient)
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Fhello",
            )
            sess.add(pwal)
            await sess.commit()
        getattr(network, "__mixnet_connected").set()
        getattr(network, "resendable_event").set()

        async def mixwal_present():
            async with persistent.asession() as sess:
                mws = (await sess.exec(
                    select(persistent.MixWAL).where(
                        persistent.MixWAL.bacap_stream == setup["bacap_stream"]
                    )
                )).all()
                return len(mws) >= 1

        await _run_loop_until(
            network.send_resendable_plaintexts(fake_thinclient),
            mixwal_present,
            timeout=5.0,
        )
        assert await mixwal_present()
        assert fake_thinclient.call_count("encrypt_write") >= 1

    @pytest.mark.asyncio
    async def test_resend_queue_guards_against_duplicate_dispatch(
        self, fake_thinclient,
    ):
        setup = await _insert_write_setup(fake_thinclient)
        # Pre-mark the bacap_stream as in-flight so the second pass would
        # otherwise re-dispatch.
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Fhi",
            )
            sess.add(pwal)
            await sess.commit()
        # Add the stream to the resend queue directly; the loop should skip it.
        getattr(network, "__resend_queue").add(setup["bacap_stream"])
        getattr(network, "resendable_event").set()
        getattr(network, "__mixnet_connected").set()

        async def quitter():
            for _ in range(40):
                await asyncio.sleep(0)
            network.shutdown()

        asyncio.create_task(quitter())
        try:
            await asyncio.wait_for(
                network.send_resendable_plaintexts(fake_thinclient),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            pass
        # find_resendable filters out bacap_streams already in the queue,
        # so encrypt_write must not have been called for the dup.
        assert fake_thinclient.call_count("encrypt_write") == 0

    @pytest.mark.asyncio
    async def test_indirection_pwal_fills_read_cap_before_dispatch(
        self, fake_thinclient,
    ):
        """An indirection PWAL has an empty bacap_payload and points at
        a ReadCapWAL via its `indirection` column. Once that
        ReadCapWAL's read_cap has been provisioned, the loop should
        fill the PWAL's bacap_payload with b'I' + read_cap and
        dispatch it. Pins both the persisted payload and the wire
        shape of the resulting encrypt_write call."""
        # The target stream the indirection points at.
        target = await _insert_write_setup(
            fake_thinclient, conv_name="target", peer_name="target_self",
            seed=b"\xab" * 32,
        )
        # The PWAL that should be filled in lives on a *different*
        # stream and references the target stream's ReadCapWAL via
        # `indirection`. Set up that second stream too so the dispatch
        # has a populated WriteCapWAL to encrypt against.
        host = await _insert_write_setup(
            fake_thinclient, conv_name="host", peer_name="host_self",
            seed=b"\xcd" * 32,
        )
        async with persistent.asession() as sess:
            release = persistent.PlaintextWAL(
                bacap_stream=host["bacap_stream"],
                conversation_id=host["conversation_id"],
                bacap_payload=b"",
                indirection=target["bacap_stream"],
            )
            sess.add(release)
            await sess.commit()
            await sess.refresh(release)
            release_id = release.id
        getattr(network, "__mixnet_connected").set()
        getattr(network, "resendable_event").set()

        def encrypt_write_seen():
            return fake_thinclient.call_count("encrypt_write") >= 1

        await _run_loop_until(
            network.send_resendable_plaintexts(fake_thinclient),
            encrypt_write_seen,
            timeout=5.0,
        )
        assert encrypt_write_seen()
        last = fake_thinclient.last_call("encrypt_write")
        # The plaintext sent must be exactly b'I' followed by the
        # target stream's read_cap.
        expected = b"I" + target["read_cap"]
        assert last["plaintext"] == expected
        # The PWAL row itself was persisted with the filled payload,
        # so a restart-from-disk would resume in the same state.
        async with persistent.asession() as sess:
            row = await sess.get(persistent.PlaintextWAL, release_id)
            assert row is not None
            assert row.bacap_payload == expected

    @pytest.mark.asyncio
    async def test_indirection_pwal_prepends_total_chunk_count_when_known(
        self, fake_thinclient,
    ):
        """When the target ReadCapWAL carries a known
        substream_total_chunks (set by models.serialize on a multi-chunk
        file), the filled-in I-chunk is b'I' + 4-byte BE count + read_cap
        so the reader can render download progress as n/total."""
        target = await _insert_write_setup(
            fake_thinclient, conv_name="target", peer_name="target_self",
            seed=b"\xab" * 32,
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, target["bacap_stream"])
            rcw.substream_total_chunks = 3  # two C-chunks + final F
            sess.add(rcw)
            await sess.commit()
        host = await _insert_write_setup(
            fake_thinclient, conv_name="host", peer_name="host_self",
            seed=b"\xcd" * 32,
        )
        async with persistent.asession() as sess:
            release = persistent.PlaintextWAL(
                bacap_stream=host["bacap_stream"],
                conversation_id=host["conversation_id"],
                bacap_payload=b"",
                indirection=target["bacap_stream"],
            )
            sess.add(release)
            await sess.commit()
            await sess.refresh(release)
            release_id = release.id
        getattr(network, "__mixnet_connected").set()
        getattr(network, "resendable_event").set()

        def encrypt_write_seen():
            return fake_thinclient.call_count("encrypt_write") >= 1

        await _run_loop_until(
            network.send_resendable_plaintexts(fake_thinclient),
            encrypt_write_seen,
            timeout=5.0,
        )
        assert encrypt_write_seen()
        expected = b"I" + (3).to_bytes(4, "big") + target["read_cap"]
        last = fake_thinclient.last_call("encrypt_write")
        assert last["plaintext"] == expected
        assert len(expected) == 1 + 4 + 136
        async with persistent.asession() as sess:
            row = await sess.get(persistent.PlaintextWAL, release_id)
            assert row is not None
            assert row.bacap_payload == expected


# ---------------------------------------------------------------------------
# disconnect / reconnect ride-through
# ---------------------------------------------------------------------------


class TestDisconnectPauseAndResume:
    """The staging loops gate every iteration on __mixnet_connected so a
    kpclientd reconnect or a transient mixnet outage cleanly pauses
    and resumes them. (drain_mixwal2 is the exception: it casts reads
    regardless so the daemon's own ARQ can ride out a link flap, while
    writes keep waiting for the gate.) These tests flip the connection
    status mid-flight and assert that the staging loops respect the gate."""

    @pytest.mark.asyncio
    async def test_send_resendable_does_not_dispatch_while_disconnected(
        self, fake_thinclient,
    ):
        setup = await _insert_write_setup(fake_thinclient)
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Fpause",
            )
            sess.add(pwal)
            await sess.commit()
        # Begin disconnected. resendable_event is set but the loop
        # should still wait at the connection gate.
        await network.on_connection_status({"is_connected": False, "err": None})
        getattr(network, "resendable_event").set()

        loop_task = asyncio.create_task(
            network.send_resendable_plaintexts(fake_thinclient),
        )
        try:
            # Give the loop a moment; gate should block.
            for _ in range(50):
                await asyncio.sleep(0)
            assert fake_thinclient.call_count("encrypt_write") == 0
            # Reconnect: the loop should now wake and dispatch.
            await network.on_connection_status({"is_connected": True, "err": None})
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("encrypt_write") >= 1:
                    break
            assert fake_thinclient.call_count("encrypt_write") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_drain_mixwal2_does_not_dispatch_while_disconnected(
        self, fake_thinclient,
    ):
        setup = await _set_up_write_flow(fake_thinclient)
        await network.on_connection_status({"is_connected": False, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()

        loop_task = asyncio.create_task(network.drain_mixwal2(fake_thinclient))
        try:
            for _ in range(50):
                await asyncio.sleep(0)
            assert fake_thinclient.call_count("start_resending_encrypted_message") == 0
            await network.on_connection_status({"is_connected": True, "err": None})
            # Re-trigger the mixwal-updated event so the loop's second
            # wait fires straight away rather than spinning on its
            # 15-second timeout.
            getattr(network, "__mixwal_updated").set()
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("start_resending_encrypted_message") >= 1:
                    break
            assert fake_thinclient.call_count("start_resending_encrypted_message") >= 1
            last = fake_thinclient.last_call("start_resending_encrypted_message")
            assert last["envelope_hash"] == setup["wcr"].envelope_hash
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_readables_to_mixwal_does_not_poll_while_disconnected(
        self, fake_thinclient,
    ):
        await _insert_write_setup(fake_thinclient)
        await network.on_connection_status({"is_connected": False, "err": None})
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "readables_to_mixwal_event").set()

        loop_task = asyncio.create_task(
            network.readables_to_mixwal(fake_thinclient),
        )
        try:
            for _ in range(50):
                await asyncio.sleep(0)
            assert fake_thinclient.call_count("encrypt_read") == 0
            await network.on_connection_status({"is_connected": True, "err": None})
            getattr(network, "readables_to_mixwal_event").set()
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("encrypt_read") >= 1:
                    break
            assert fake_thinclient.call_count("encrypt_read") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_readables_to_mixwal_retries_after_idle_bound_while_latch_unset(
        self, fake_thinclient, monkeypatch,
    ):
        """With `__mixnet_connected` cleared and never re-set, the loop must
        still attempt an arming pass after the idle bound."""
        await _insert_write_setup(fake_thinclient)
        getattr(network, "__resend_queue_populated").set()  # loop prelude
        monkeypatch.setattr(network, "_CONNECTION_IDLE_RETRY_S", 0.05)
        monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.05)
        # Latch cleared once (restart), and no reconnect ever reports in.
        await network.on_connection_status({"is_connected": False, "err": None})
        getattr(network, "readables_to_mixwal_event").set()
        loop_task = asyncio.create_task(
            network.readables_to_mixwal(fake_thinclient),
        )
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("encrypt_read") >= 1:
                    break
            assert fake_thinclient.call_count("encrypt_read") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_readables_to_mixwal_rearms_on_sweep_when_event_never_fires(
        self, fake_thinclient, monkeypatch,
    ):
        """The arming loop must run on the `_ARMING_SWEEP_S` cadence even when
        the event is never re-set again."""
        await _insert_write_setup(fake_thinclient)
        getattr(network, "__resend_queue_populated").set()  # loop prelude
        monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.05)
        # Mixnet connected, latch set; event starts clear and stays clear.
        await network.on_connection_status({"is_connected": True, "err": None})
        event = getattr(network, "readables_to_mixwal_event")
        event.clear()
        loop_task = asyncio.create_task(
            network.readables_to_mixwal(fake_thinclient),
        )
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("encrypt_read") >= 1:
                    break
            assert fake_thinclient.call_count("encrypt_read") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_send_resendable_retries_after_idle_bound_while_latch_unset(
        self, fake_thinclient, monkeypatch,
    ):
        """Same bounded-gate behaviour for the write path: pending plaintext
        is dispatched after the idle bound even with the latch cleared."""
        setup = await _insert_write_setup(fake_thinclient)
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Fstill here",
            )
            sess.add(pwal)
            await sess.commit()
        monkeypatch.setattr(network, "_CONNECTION_IDLE_RETRY_S", 0.05)
        await network.on_connection_status({"is_connected": False, "err": None})
        getattr(network, "resendable_event").set()
        loop_task = asyncio.create_task(
            network.send_resendable_plaintexts(fake_thinclient),
        )
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake_thinclient.call_count("encrypt_write") >= 1:
                    break
            assert fake_thinclient.call_count("encrypt_write") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_drain_mixwal_write_single_swallows_offline_mid_call(
        self, fake_thinclient,
    ):
        """A daemon-side socket drop mid-call surfaces as
        ThinClientOfflineError on a single drain attempt. The retry
        path (the surrounding drain loop) handles re-trying once the
        connection comes back; the per-call helper just needs to swallow
        cleanly and leave the MixWAL row in place for the next pass."""
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", ThinClientOfflineError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        # MW preserved; the next loop iteration after reconnect will
        # have another go.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.asyncio
    async def test_write_dispatch_has_no_counter_probe(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", ThinClientOfflineError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        assert fake_thinclient.call_count("get_message_box_index_counter") == 0
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 1
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None

    @pytest.mark.real_sleeps
    @pytest.mark.asyncio
    async def test_drain_mixwal2_survives_counter_offline_and_retries(
        self, fake_thinclient,
    ):
        """A transient link drop when the drain loop picks up a fresh
        write must not kill the loop (the old debug-log probe ran an
        awaited counter call in the loop body, so one offline raise killed
        ALL draining) and must not strand the stream: the next pass
        re-sweeps it to a successful ACK."""
        setup = await _set_up_write_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "get_message_box_index_counter", ThinClientOfflineError(),
        )
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        async def mw_drained():
            async with persistent.asession() as sess:
                return await sess.get(persistent.MixWAL, setup["mw_id"]) is None

        loop_task = asyncio.create_task(network.drain_mixwal2(fake_thinclient))
        try:
            for _ in range(1000):
                await asyncio.sleep(0.02)
                if await mw_drained():
                    break
            assert await mw_drained()
            # Failed probe originally, then a successful re-probe + ACK.
            assert fake_thinclient.call_count("get_message_box_index_counter") >= 2
            assert fake_thinclient.call_count("start_resending_encrypted_message") >= 1
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(loop_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                loop_task.cancel()

    @pytest.mark.asyncio
    async def test_drain_mixwal_read_single_swallows_offline_mid_call(
        self, fake_thinclient,
    ):
        setup = await _set_up_read_flow(fake_thinclient)
        fake_thinclient.inject_error(
            "start_resending_encrypted_message", ThinClientOfflineError(),
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now={setup["bacap_stream"]},
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None


# ---------------------------------------------------------------------------
# reconnect
# ---------------------------------------------------------------------------


class TestReconnect:
    @pytest.mark.asyncio
    async def test_returns_started_client(self, fake_thinclient, monkeypatch):
        # Stub ThinClientConfig so we don't read the real toml from disk.
        class _StubConfig:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                assert kwargs["on_daemon_disconnected"] is network.on_daemon_disconnected

        monkeypatch.setattr(network, "ThinClientConfig", _StubConfig)
        monkeypatch.setattr(network, "ThinClient", lambda cfg: fake_thinclient)
        client = await network.reconnect()
        assert client is fake_thinclient
        assert fake_thinclient.started is True
        assert fake_thinclient.started_loop is asyncio.get_running_loop()


# ---------------------------------------------------------------------------
# start_background_threads (via the live_network fixture)
# ---------------------------------------------------------------------------


class TestStartBackgroundThreads:
    @pytest.mark.asyncio
    async def test_smoke_orchestrates_and_shuts_down(self, live_network):
        """All four background loops boot, register, and shut down clean
        when network.shutdown() is called. Exercises the gather +
        as_completed orchestration that nothing else touches."""
        # live_network has already started start_background_threads in a
        # task. Confirm __resend_queue_populated was set by the
        # send_resendable_plaintexts boot (the fixture waits on it).
        assert getattr(network, "__resend_queue_populated").is_set()
        # The loops are running; teardown of the fixture will call
        # network.shutdown() and wait for the orchestrator to exit.
        assert not live_network.task.done()


# ---------------------------------------------------------------------------
# Send-loop resilience: a stuck dispatched send must not wedge the
# orchestrator or the other streams.
# ---------------------------------------------------------------------------


class TestSendLoopResilience:
    @pytest.mark.real_sleeps
    @pytest.mark.asyncio
    async def test_one_stuck_send_does_not_wedge_orchestrator(
        self, live_network, fake_thinclient,
    ):
        """A single drain_mixwal_write_single hanging forever on an
        un-ACK'd envelope must not stop drain_mixwal2 from continuing
        to dispatch other streams, and must not propagate as an
        exception into start_background_threads.

        Uses real sleeps so SentLog.mark_sent's mixed sync/async
        session work for stream B settles without racing the test's
        polling under coverage instrumentation.
        """
        # Stream A: pre-stage a MW whose courier ACK we will withhold.
        setup_a = await _set_up_write_flow(
            fake_thinclient,
            plaintext=b"Fstuck",
            seed=b"\xaa" * 32,
            conv_name="A",
            peer_name="a-self",
        )
        fake_thinclient.hold_ack(setup_a["wcr"].envelope_hash)

        # Stream B: a separate stream whose dispatch should proceed
        # normally while A is wedged.
        setup_b = await _set_up_write_flow(
            fake_thinclient,
            plaintext=b"Fprogresses",
            seed=b"\xbb" * 32,
            conv_name="B",
            peer_name="b-self",
        )

        # Wake drain_mixwal2 to pick up both rows.
        getattr(network, "__mixwal_updated").set()

        async def b_drained() -> bool:
            async with persistent.asession() as sess:
                return await sess.get(persistent.MixWAL, setup_b["mw_id"]) is None

        # Wait for B to settle. Real sleeps so SentLog.mark_sent's
        # async/sync session interplay has wall-clock time to commit.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if await b_drained():
                break

        assert await b_drained(), (
            "stream B's MixWAL should have drained while A was stuck"
        )

        # A is still in flight: its MW remains, its envelope was issued
        # to the daemon (call_log records it), but no ACK has come back.
        async with persistent.asession() as sess:
            mw_a = await sess.get(persistent.MixWAL, setup_a["mw_id"])
            assert mw_a is not None
        assert fake_thinclient.call_count("start_resending_encrypted_message") >= 2

        # And the orchestrator is alive, which is the load-bearing
        # claim of this whole test: a stuck dispatched task must NOT
        # kill start_background_threads. The fixture teardown will
        # call network.shutdown() and confirm it exits cleanly.
        assert not live_network.task.done()


# ---------------------------------------------------------------------------
# test_keypair (developer smoke helper)
# ---------------------------------------------------------------------------


class TestTestKeypairHelper:
    @pytest.mark.asyncio
    async def test_keypair_round_trip(
        self, fake_thinclient, monkeypatch,
    ):
        """`network.test_keypair` is a developer smoke helper that exercises
        encrypt_write → start_resending → encrypt_read → start_resending
        in one shot. It is wired only by hand in REPL sessions, so this
        test exists to keep its bytecode warm and surface a future
        signature drift."""
        # The helper sleeps 20 seconds in the middle; our autouse
        # fast_asyncio_sleep collapses that.
        kp = await fake_thinclient.new_keypair(b"\x77" * 32)
        # Pre-store the box at the keypair's first index so the read leg
        # has something to return; otherwise the box-id lookup would
        # raise BoxIDNotFoundError.
        await network.test_keypair(fake_thinclient, kp.write_cap, kp.read_cap)


class TestDoneCallbackPrimitive:
    """Direct tests of the shared fire-and-forget done-callback primitive
    (`_done_callback`) that `_on_write_done` / `_on_read_done` / `on_error`
    are now thin wrappers over: the exception is consumed and logged, never
    re-raised (no asyncio "Exception in callback" spam), cancellation runs
    `on_cancel`, success runs neither hook."""

    class _Recorder:
        def __init__(self):
            self.cancelled = []
            self.errors = []

        def on_cancel(self):
            self.cancelled.append("cancel")

        def on_error(self, exc):
            self.errors.append(exc)

    @pytest.mark.asyncio
    async def test_exception_fires_on_error_and_logs_with_exc_info(
        self, caplog,
    ):
        rec = self._Recorder()
        loop = asyncio.get_running_loop()
        handler_calls = []
        prev_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop_, ctx: handler_calls.append(ctx))
        try:
            async def boom():
                raise RuntimeError("nope")

            with caplog.at_level(logging.ERROR, logger="katzen.network"):
                task = asyncio.create_task(boom())
                network._done_callback(task, desc="test drain", on_error=rec.on_error)
                with pytest.raises(RuntimeError):
                    await task
                await asyncio.sleep(0)  # let the done_callback run
        finally:
            loop.set_exception_handler(prev_handler)

        assert len(rec.errors) == 1 and isinstance(rec.errors[0], RuntimeError)
        assert handler_calls == []  # nothing re-raised into the loop
        assert any(
            r.exc_info and isinstance(r.exc_info[1], RuntimeError)
            and "test drain" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_cancellation_fires_on_cancel_only(self, caplog):
        rec = self._Recorder()

        async def sleeps():
            await asyncio.Event().wait()

        with caplog.at_level(logging.ERROR, logger="katzen.network"):
            task = asyncio.create_task(sleeps())
            network._done_callback(task, desc="test drain",
                                   on_cancel=rec.on_cancel, on_error=rec.on_error)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)

        assert rec.cancelled == ["cancel"]
        assert rec.errors == []
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_success_runs_neither_hook(self, caplog):
        rec = self._Recorder()

        async def ok():
            return 42

        with caplog.at_level(logging.ERROR, logger="katzen.network"):
            task = asyncio.create_task(ok())
            network._done_callback(task, desc="test drain",
                                   on_cancel=rec.on_cancel, on_error=rec.on_error)
            assert await task == 42
            await asyncio.sleep(0)

        assert rec.cancelled == []
        assert rec.errors == []
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_exception_passes_exc_to_on_error_hook(self):
        rec = self._Recorder()

        async def boom():
            raise RuntimeError("nope")

        task = asyncio.create_task(boom())
        network._done_callback(task, desc="test drain", on_error=rec.on_error)
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

        assert isinstance(rec.errors[0], RuntimeError)
        assert str(rec.errors[0]) == "nope"


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_initial_read_setup_recovers_without_advancing(
    fake_thinclient, monkeypatch, disconnect,
):
    setup = await _insert_write_setup(fake_thinclient)
    original = fake_thinclient.encrypt_read
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def encrypt_read(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(**kwargs)

    monkeypatch.setattr(fake_thinclient, "encrypt_read", encrypt_read)
    monkeypatch.setattr(network, "_DAEMON_RPC_TIMEOUT_SECONDS", 0.05)
    await network.on_connection_status({"is_connected": True})
    getattr(network, "__resend_queue_populated").set()
    task = asyncio.create_task(network.readables_to_mixwal(fake_thinclient))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if disconnect:
            await network.on_daemon_disconnected({"is_graceful": False})
            await network.on_connection_status({"is_connected": True})
        await asyncio.wait_for(cancelled.wait(), 2)

        async def armed():
            while True:
                async with persistent.asession() as sess:
                    rows = (await sess.exec(select(persistent.MixWAL))).all()
                    if rows:
                        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
                        assert len(rows) == 1
                        assert rows[0].current_message_index == setup["first_message_index"]
                        assert rcw.next_index == setup["first_message_index"]
                        return
                await asyncio.sleep(0)

        await asyncio.wait_for(armed(), 2)
        assert calls == 2
        assert fake_thinclient.call_count("get_message_box_index_counter") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_read_setup_timeout_preserves_pending_read(fake_thinclient, monkeypatch):
    payload = _make_F_payload("retry setup")
    setup = await _set_up_read_flow(fake_thinclient, plaintext=payload)
    original = fake_thinclient.encrypt_read
    cancelled = asyncio.Event()

    async def stalled(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(fake_thinclient, "encrypt_read", stalled)
    monkeypatch.setattr(network, "_DAEMON_RPC_TIMEOUT_SECONDS", 0.02)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
    draining = {setup["bacap_stream"]}
    await asyncio.wait_for(network.drain_mixwal_read_single(
        connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
        mw=mw, draining_right_now=draining,
    ), 2)
    assert cancelled.is_set()
    assert not draining
    assert fake_thinclient.call_count("cancel_resending_encrypted_message") == 0
    async with persistent.asession() as sess:
        assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw.next_index == setup["first_message_index"]
    monkeypatch.setattr(fake_thinclient, "encrypt_read", original)
    draining.add(setup["bacap_stream"])
    await network.drain_mixwal_read_single(
        connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
        mw=mw, draining_right_now=draining,
    )
    async with persistent.asession() as sess:
        assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
        logs = (await sess.exec(select(persistent.ConversationLog))).all()
        assert len(logs) == 1 and logs[0].payload == payload


@pytest.mark.asyncio
async def test_daemon_restart_signals_read_recovery():
    await network.on_connection_status({"is_connected": True})
    marker = network._reconnect_event
    await network.on_daemon_disconnected({"is_graceful": False})
    assert not getattr(network, "__mixnet_connected").is_set()
    await network.on_connection_status({"is_connected": True})
    assert marker.is_set()
    assert not network._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_read_reply_cancellation_joins_owned_tasks(fake_thinclient, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stalled(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(fake_thinclient, "start_resending_encrypted_message", stalled)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(network._await_read_reply(
        fake_thinclient, read_watchdog_s=60, reconnect_grace_s=30,
        bacap_uuid=uuid.uuid4(),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert asyncio.all_tasks() == baseline


@pytest.mark.asyncio
async def test_absent_box_returns_to_polling_without_advancing(monkeypatch, fake_thinclient):
    setup = await _set_up_read_flow(fake_thinclient)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])

    original_read = fake_thinclient.start_resending_encrypted_message

    async def read(**kwargs):
        if kwargs.get("no_retry_on_box_id_not_found"):
            raise network.BoxIDNotFoundError()
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_thinclient, "start_resending_encrypted_message", read)
    draining = {mw.bacap_stream}
    await asyncio.wait_for(network.drain_mixwal_read_single(
        connection=fake_thinclient,
        rcw_read_cap=setup["read_cap"],
        mw=mw,
        draining_right_now=draining,
    ), 0.05)
    assert not draining
    async with persistent.asession() as sess:
        stored = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert stored.current_message_index == mw.current_message_index
        rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
        assert rcw.next_index == mw.current_message_index
    monkeypatch.setattr(fake_thinclient, "start_resending_encrypted_message", original_read)
    draining.add(mw.bacap_stream)
    await network.drain_mixwal_read_single(
        connection=fake_thinclient,
        rcw_read_cap=setup["read_cap"],
        mw=stored,
        draining_right_now=draining,
    )
    assert not draining
    async with persistent.asession() as sess:
        assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
        rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
        assert rcw.next_index == setup["rcr"].next_message_box_index
        rows = (await sess.exec(select(persistent.ConversationLog))).all()
        assert len(rows) == 1 and rows[0].payload == _make_F_payload("hello")
class TestSafeBasename:
    @pytest.mark.parametrize("raw,expected", [
        ("ev\x00il.txt", "ev_il.txt"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ("caf\u00e9.txt", "caf_.txt"),
        ("\u4e2d\u6587.png", "__.png"),
        ("ok name (1).png", "ok name (1).png"),
        ("..hidden", "hidden"),
        ("", "unnamed"),
    ])
    def test_reduces_a_peer_name_to_seven_bit_ascii(self, raw, expected):
        got = network._safe_basename(raw)
        assert got == expected
        assert all(ord(c) < 128 for c in got)
class TestUnprocessableContentDoesNotWedgeTheStream:
    @pytest.mark.asyncio
    async def test_a_raising_handler_advances_and_drops_the_row(
        self, fake_thinclient, monkeypatch, caplog
    ):
        setup = await _set_up_read_flow(
            fake_thinclient, plaintext=_make_F_payload("poison"),
        )
        async def boom(*_a, **_k):
            raise ValueError("undecodable peer content")
        monkeypatch.setattr(network.conversation_handlers, "dispatch", boom)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
            before = (await sess.get(
                persistent.ReadCapWAL, setup["bacap_stream"])).next_index
        draining: set = {setup["bacap_stream"]}
        with caplog.at_level(logging.ERROR, logger="katzen.network"):
            await network.drain_mixwal_read_single(
                connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
                mw=mw, draining_right_now=draining,
            )
        assert any("dropping unprocessable message" in r.message
                   for r in caplog.records), [r.message for r in caplog.records]
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None, (
                "row retained: the stream is wedged, no later message arrives"
            )
            after = (await sess.get(
                persistent.ReadCapWAL, setup["bacap_stream"])).next_index
            assert after != before, "index did not advance past the poisoned box"
        assert setup["bacap_stream"] not in draining


# ---------------------------------------------------------------------------
# Outbound substream (upload) transfer events
# ---------------------------------------------------------------------------


async def _set_up_upload_flow(
    fake, *, total_chunks: int = 3, chunks_present: int = 2,
):
    """Build an outbound substream: an agg WriteCapWAL, its indirection
    ReadCapWAL (carrying ``substream_total_chunks``), the gated I-chunk on the
    main stream, ``chunks_present`` agg C/F PWALs, and a write-MixWAL for the
    first chunk so drain_mixwal_write_single can be driven."""
    setup = await _insert_write_setup(fake, active=False)
    agg_kp = await fake.new_keypair(b"\x77" * 32)
    agg = uuid.uuid4()
    rcw_id = uuid.uuid4()
    first_chunk_id = uuid.uuid4()
    i_chunk_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(persistent.WriteCapWAL(
            id=agg, write_cap=agg_kp.write_cap,
            next_index=agg_kp.first_message_index,
        ))
        sess.add(persistent.ReadCapWAL(
            id=rcw_id, write_cap_id=agg,
            read_cap=agg_kp.read_cap, next_index=agg_kp.first_message_index,
            substream_total_chunks=total_chunks,
        ))
        sess.add(persistent.PlaintextWAL(
            id=i_chunk_id, bacap_stream=setup["bacap_stream"],
            conversation_id=setup["conversation_id"], bacap_payload=b"",
            indirection=rcw_id,
        ))
        for i in range(chunks_present):
            sess.add(persistent.PlaintextWAL(
                id=first_chunk_id if i == 0 else uuid.uuid4(),
                bacap_stream=agg,
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Cchunk",
            ))
        await sess.commit()
    wcr = await fake.encrypt_write(
        plaintext=b"Cchunk", write_cap=agg_kp.write_cap,
        message_box_index=agg_kp.first_message_index,
    )
    mw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(persistent.MixWAL(
            id=mw_id, plaintextwal=first_chunk_id, bacap_stream=agg,
            envelope_hash=wcr.envelope_hash,
            encrypted_payload=wcr.message_ciphertext,
            envelope_descriptor=wcr.envelope_descriptor,
            current_message_index=agg_kp.first_message_index,
            next_message_index=wcr.next_message_box_index,
            is_read=False,
        ))
        await sess.commit()
    setup.update({"agg": agg, "rcw_id": rcw_id, "mw_id": mw_id,
                  "i_chunk_id": i_chunk_id})
    return setup


def _drain_progress_queue() -> None:
    while not network.substream_progress_queue.empty():
        network.substream_progress_queue.get_nowait()


class TestUploadTransferEvents:
    @pytest.mark.asyncio
    async def test_notify_outbound_chat_sent_announces_upload(
        self, fake_thinclient,
    ):
        _drain_progress_queue()
        setup = await _insert_write_setup(fake_thinclient, conv_name="carol-conv")
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        rcw = persistent.ReadCapWAL(
            id=rcw_id, write_cap_id=agg, substream_total_chunks=3,
        )
        i_chunk = persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=setup["bacap_stream"],
            conversation_id=setup["conversation_id"], bacap_payload=b"",
            indirection=rcw_id,
        )
        chunk = persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=agg,
            conversation_id=setup["conversation_id"], bacap_payload=b"Cx",
        )
        await network.notify_outbound_chat_sent(
            conversation_id=setup["conversation_id"],
            conversation_peer_id=setup["peer_id"],
            new_write_caps=[agg],
            db_entries=[chunk, rcw, i_chunk],
            payload=b"Flocal",
            final_pwal_id=i_chunk.id,
        )
        event = network.substream_progress_queue.get_nowait()
        # chunk payload is b"Cx": one effective payload byte after the
        # 1-byte chunk-type prefix. The local payload is not a file marker,
        # so there is no basename.
        assert event == (
            "upload_started", rcw_id, setup["conversation_id"], 3, 1,
            "carol-conv", None,
        )
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_write_ack_emits_upload_piece(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=3, chunks_present=2,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["agg"]},
        )
        event = network.substream_progress_queue.get_nowait()
        # each b"Cchunk" payload is 5 effective bytes (6 minus the type byte);
        # one of the two present chunks was ACK'd, leaving 5 bytes outstanding.
        assert event == ("upload_piece", setup["rcw_id"], 2, 5)
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_write_ack_completes_upload(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=1, chunks_present=1,
        )
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["agg"]},
        )
        event = network.substream_progress_queue.get_nowait()
        assert event == ("upload_completed", setup["rcw_id"])

    @pytest.mark.asyncio
    async def test_main_stream_ack_emits_no_upload_event(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_write_flow(fake_thinclient)
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        await network.drain_mixwal_write_single(
            fake_thinclient, mw, {setup["bacap_stream"]},
        )
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_pause_upload_marks_stream_and_clears_pending_write(
        self, fake_thinclient,
    ):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=3, chunks_present=2,
        )
        await network.pause_upload(rcw_id=setup["rcw_id"])
        async with persistent.asession() as sess:
            wcw = await sess.get(persistent.WriteCapWAL, setup["agg"])
            assert wcw.paused is True
            # The pending write MixWAL was deleted so the sweep cannot re-cast.
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            # Chunk PlaintextWAL rows survive so resume can re-encrypt.
            remaining = (await sess.exec(
                select(persistent.PlaintextWAL).where(
                    persistent.PlaintextWAL.bacap_stream == setup["agg"],
                )
            )).all()
            assert len(remaining) == 2
        assert network.substream_progress_queue.get_nowait() == (
            "upload_paused", setup["rcw_id"],
        )

    @pytest.mark.asyncio
    async def test_resume_upload_clears_marker(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=3, chunks_present=2,
        )
        await network.pause_upload(rcw_id=setup["rcw_id"])
        _drain_progress_queue()
        await network.resume_upload(rcw_id=setup["rcw_id"])
        async with persistent.asession() as sess:
            wcw = await sess.get(persistent.WriteCapWAL, setup["agg"])
            assert wcw.paused is False
        assert network.substream_progress_queue.get_nowait() == (
            "upload_resumed", setup["rcw_id"],
        )

    @pytest.mark.asyncio
    async def test_pause_upload_ignores_a_non_upload_rcw(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_write_flow(fake_thinclient)
        # The main stream's own-peer ReadCapWAL has no write_cap_id, so it is
        # not an upload and pause is a no-op.
        await network.pause_upload(rcw_id=setup["bacap_stream"])
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_cancel_upload_removes_rows_and_bubble(self, fake_thinclient):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=3, chunks_present=2,
        )
        async with persistent.asession() as sess:
            sess.add(persistent.ConversationLog(
                id=uuid.uuid4(), conversation_id=setup["conversation_id"],
                conversation_peer_id=setup["peer_id"],
                conversation_order=0, payload=b"Flocal",
                network_status=1, outgoing_pwal=setup["i_chunk_id"],
            ))
            await sess.commit()
        await network.cancel_upload(rcw_id=setup["rcw_id"])
        async with persistent.asession() as sess:
            assert await sess.get(
                persistent.PlaintextWAL, setup["i_chunk_id"]) is None
            assert await sess.get(
                persistent.WriteCapWAL, setup["agg"]) is None
            assert await sess.get(
                persistent.ReadCapWAL, setup["rcw_id"]) is None
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            agg_pwals = (await sess.exec(
                select(persistent.PlaintextWAL).where(
                    persistent.PlaintextWAL.bacap_stream == setup["agg"],
                )
            )).all()
            assert agg_pwals == []
            convlogs = (await sess.exec(
                select(persistent.ConversationLog).where(
                    persistent.ConversationLog.outgoing_pwal
                    == setup["i_chunk_id"],
                )
            )).all()
            assert convlogs == []
        assert network.substream_progress_queue.get_nowait() == (
            "upload_cancelled", setup["rcw_id"],
        )

    @pytest.mark.asyncio
    async def test_cancel_upload_refuses_once_i_chunk_is_gone(
        self, fake_thinclient,
    ):
        _drain_progress_queue()
        setup = await _set_up_upload_flow(
            fake_thinclient, total_chunks=3, chunks_present=2,
        )
        # Simulate the substream having completed and the I-chunk dispatched:
        # the upload is no longer cancellable.
        async with persistent.asession() as sess:
            i_chunk = await sess.get(
                persistent.PlaintextWAL, setup["i_chunk_id"])
            await sess.delete(i_chunk)
            await sess.commit()
        await network.cancel_upload(rcw_id=setup["rcw_id"])
        async with persistent.asession() as sess:
            remaining = (await sess.exec(
                select(persistent.PlaintextWAL).where(
                    persistent.PlaintextWAL.bacap_stream == setup["agg"],
                )
            )).all()
            assert len(remaining) == 2
        assert network.substream_progress_queue.empty()
