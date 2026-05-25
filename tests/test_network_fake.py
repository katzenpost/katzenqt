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
import uuid

import pytest
from sqlmodel import select

from katzenpost_thinclient import (
    BACAPDecryptionFailedError,
    StartResendingCancelledError,
    ThinClientOfflineError,
)
from katzenpost_thinclient.core import MKEMDecryptionFailedError

from katzenqt import network, persistent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_keypair(fake, seed: bytes = b"\x11" * 32):
    """Return a fresh KeypairResult from the fake. Centralised so the
    tests do not have to import network.create_new_keypair themselves."""
    return await fake.new_keypair(seed)


async def _insert_write_setup(
    fake, *, conv_name: str = "demo", peer_name: str = "self",
    seed: bytes = b"\x33" * 32,
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
            active=True,
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
# courier_destination_exists with the fake's PKI
# ---------------------------------------------------------------------------


class TestCourierDestinationExistsFakeBacked:
    def test_default_courier_is_present(self, fake_thinclient):
        dest = fake_thinclient.couriers[0][0]
        assert network.courier_destination_exists(fake_thinclient, dest) is True

    def test_missing_after_removal(self, fake_thinclient):
        dest = fake_thinclient.couriers[0][0]
        fake_thinclient.remove_courier(dest)
        assert network.courier_destination_exists(fake_thinclient, dest) is False


# ---------------------------------------------------------------------------
# Scaffolding helpers for drain_mixwal_{write,read}_single
# ---------------------------------------------------------------------------


async def _set_up_write_flow(fake, *, plaintext: bytes = b"Fhello"):
    """Build the DB rows + fake envelope state representing 'we have just
    encrypted a write and persisted it to MixWAL; ready to drain'.

    Returns dict with keys: bacap_stream, write_cap, read_cap,
    first_message_index, conversation_id, peer_id, mw_id, pwal_id.
    """
    setup = await _insert_write_setup(fake)
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


async def _set_up_read_flow(fake, *, plaintext: bytes = b"Fhello"):
    """Like _set_up_write_flow but also pre-stores the box and builds a
    read-MixWAL row so drain_mixwal_read_single can be tested.
    """
    setup = await _insert_write_setup(fake)
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
        # draining_right_now intentionally NOT released — the docstring
        # admits this is rough, but the test pins the current behaviour
        # so a future cleanup is observable.
        assert setup["bacap_stream"] in draining

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


# ---------------------------------------------------------------------------
# drain_mixwal_read_single
# ---------------------------------------------------------------------------


class TestDrainMixwalReadSingle:
    @pytest.mark.asyncio
    async def test_success_with_final_prefix(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Fpayload")
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient,
            rcw_read_cap=setup["read_cap"],
            mw=mw,
            draining_right_now=draining,
        )
        # MW deleted, ConversationLog appended, ReceivedPiece written,
        # RCW.next_index advanced.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
            log = (await sess.exec(select(persistent.ConversationLog))).all()
            assert len(log) == 1 and log[0].payload == b"Fpayload"
            pieces = (await sess.exec(select(persistent.ReceivedPiece))).all()
            assert len(pieces) == 1 and pieces[0].chunk_type == b"F"
            rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
            assert rcw.next_index == setup["rcr"].next_message_box_index
        assert setup["bacap_stream"] not in draining

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
    async def test_courier_gone_pauses_and_gives_up(self, fake_thinclient):
        setup = await _set_up_read_flow(fake_thinclient)
        # Remove the courier so courier_destination_exists returns False.
        fake_thinclient.remove_courier(fake_thinclient.couriers[0][0])
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        draining: set = {setup["bacap_stream"]}
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now=draining,
        )
        # MW still present (we did not even try); start_resending never called.
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 0
        assert setup["bacap_stream"] not in draining

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
            await asyncio.sleep(0)
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
        setup = await _set_up_read_flow(fake_thinclient, plaintext=b"Fhi")
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
    async def test_skips_write_mw_for_missing_courier(self, fake_thinclient):
        setup = await _set_up_write_flow(fake_thinclient)
        # Change the mw.destination to a courier that doesn't exist.
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, setup["mw_id"])
            mw.destination = b"\x99" * 32
            sess.add(mw)
            await sess.commit()
        getattr(network, "__resend_queue_populated").set()
        getattr(network, "__mixwal_updated").set()
        getattr(network, "__mixnet_connected").set()

        async def loop_idle():
            # Give the loop a chance to process and skip.
            return network.drain_mixwal2 is not None  # always truthy

        # Run a short tick: shutdown after a few yields.
        async def quitter():
            for _ in range(40):
                await asyncio.sleep(0)
            network.shutdown()

        asyncio.create_task(quitter())
        try:
            await asyncio.wait_for(
                network.drain_mixwal2(fake_thinclient), timeout=3.0,
            )
        except asyncio.TimeoutError:
            pass
        # MW still present (skipped, not drained).
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert fake_thinclient.call_count("start_resending_encrypted_message") == 0


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


# ---------------------------------------------------------------------------
# disconnect / reconnect ride-through
# ---------------------------------------------------------------------------


class TestDisconnectPauseAndResume:
    """The drain loops gate every iteration on __mixnet_connected so a
    kpclientd reconnect or a transient mixnet outage cleanly pauses
    and resumes them. These tests flip the connection status mid-flight
    and assert that the loops respect the gate."""

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
# reset_for_reconnect / second-session lifecycle
# ---------------------------------------------------------------------------


class TestReconnectReinvocation:
    """A full lifecycle should be repeatable in the same process: boot
    background loops against one ThinClient, shut them down cleanly,
    call `reset_for_reconnect()`, then boot a second session against a
    fresh ThinClient. Models the GUI's hypothetical
    File -> Reconnect menu item."""

    @staticmethod
    async def _run_one_session(fake, monkeypatch):
        import katzenpost_thinclient as ktc
        monkeypatch.setattr(
            ktc, "find_services",
            lambda capability, doc: fake.find_services(capability),
        )
        await network.on_connection_status({"is_connected": True, "err": None})
        task = asyncio.create_task(network.start_background_threads(fake))
        try:
            await asyncio.wait_for(
                getattr(network, "__resend_queue_populated").wait(),
                timeout=3.0,
            )
            assert not task.done()
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        return task

    @pytest.mark.asyncio
    async def test_two_sessions_back_to_back(self, monkeypatch):
        from tests.fakes.thinclient import FakeThinClient

        fake1 = FakeThinClient()
        task1 = await self._run_one_session(fake1, monkeypatch)
        assert task1.done()
        assert fake1.call_count("new_keypair") == 0  # no work to do

        network.reset_for_reconnect()
        # After reset, events must be fresh and clear/quiet again.
        assert not getattr(network, "__should_quit").is_set()
        assert not getattr(network, "__mixnet_connected").is_set()
        assert not getattr(network, "__resend_queue_populated").is_set()

        fake2 = FakeThinClient()
        task2 = await self._run_one_session(fake2, monkeypatch)
        assert task2.done()
        # Independent FakeThinClient: nothing from the first session
        # leaked across to the second.
        assert fake1 is not fake2

    @pytest.mark.asyncio
    async def test_second_session_can_send_a_message(self, monkeypatch):
        """After a reset, the resend pipeline still dispatches a fresh
        PWAL inserted post-reset, end-to-end through the second
        FakeThinClient."""
        from tests.fakes.thinclient import FakeThinClient
        import katzenpost_thinclient as ktc

        fake1 = FakeThinClient()
        await self._run_one_session(fake1, monkeypatch)
        network.reset_for_reconnect()

        # Second session: actually drive a send through.
        fake2 = FakeThinClient()
        setup = await _insert_write_setup(fake2)
        async with persistent.asession() as sess:
            pwal = persistent.PlaintextWAL(
                bacap_stream=setup["bacap_stream"],
                conversation_id=setup["conversation_id"],
                bacap_payload=b"Frestart",
            )
            sess.add(pwal)
            await sess.commit()

        monkeypatch.setattr(
            ktc, "find_services",
            lambda capability, doc: fake2.find_services(capability),
        )
        await network.on_connection_status({"is_connected": True, "err": None})
        task = asyncio.create_task(network.start_background_threads(fake2))
        try:
            await asyncio.wait_for(
                getattr(network, "__resend_queue_populated").wait(),
                timeout=3.0,
            )
            await network.check_for_new()
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0)
                if fake2.call_count("encrypt_write") >= 1:
                    break
            assert fake2.call_count("encrypt_write") >= 1
            # And the first session's fake saw nothing for this stream.
            assert fake1.call_count("encrypt_write") == 0
        finally:
            network.shutdown()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


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
