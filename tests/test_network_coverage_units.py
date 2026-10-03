from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast

import cbor2
import pytest
from sqlmodel import select

from katzenqt import models, network, persistent
from tests import transfer_events

if TYPE_CHECKING:
    from katzenpost_thinclient import ThinClient
from tests.stubs import ignore, returning


class _Fake(Protocol):
    async def new_keypair(self, seed: bytes) -> object: ...

    def inject_error(self, method: str, exc: Exception) -> None: ...

    async def start_resending_encrypted_message(
        self,
        **kwargs: object,
    ) -> object: ...


def _conn(fake: _Fake) -> network.ThinClient:
    return cast(network.ThinClient, fake)


def _idx(counter: int) -> bytes:
    return counter.to_bytes(8, "little") + bytes(96)


async def _make_conversation(
    name: str = "conv",
) -> tuple[int, int, uuid.UUID]:
    stream = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.ReadCapWAL(
                id=stream,
                write_cap_id=None,
                read_cap=bytes(136),
                next_index=_idx(0),
            )
        )
        peer = persistent.ConversationPeer(name="self", read_cap_id=stream)
        sess.add(peer)
        await sess.commit()
        await sess.refresh(peer)
        peer_id = int(peer.id)
        conv = persistent.Conversation(
            name=name,
            own_peer_id=peer_id,
            write_cap=stream,
        )
        sess.add(conv)
        await sess.commit()
        await sess.refresh(conv)
        conv_id = int(conv.id)
        sess.add(
            persistent.ConversationPeerLink(
                conversation_peer_id=peer_id,
                conversation_id=conv_id,
            )
        )
        await sess.commit()
    return conv_id, peer_id, stream


async def _add_mixwal(
    stream: uuid.UUID,
    *,
    is_read: bool = True,
    plaintextwal: uuid.UUID | None = None,
) -> uuid.UUID:
    mw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.MixWAL(
                id=mw_id,
                bacap_stream=stream,
                plaintextwal=plaintextwal,
                envelope_hash=uuid.uuid4().bytes,
                encrypted_payload=b"ct",
                envelope_descriptor=b"ed",
                current_message_index=_idx(0),
                next_message_index=_idx(1),
                is_read=is_read,
            )
        )
        await sess.commit()
    return mw_id


@pytest.fixture(autouse=True)
def _drain_conversation_updates() -> Iterator[None]:
    yield
    while not network.conversation_update_queue.empty():
        network.conversation_update_queue.get_nowait()


class TestPacketTelemetryCaps:
    def test_an_empty_label_is_not_recorded(self) -> None:
        stream = uuid.uuid4()
        network.set_upload_label(stream, "")
        network.set_upload_label(None, "ignored")
        assert network.upload_label(stream) is None

    def test_upload_labels_evict_the_oldest_past_the_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(network, "_UPLOAD_LABEL_CAP", 1)
        first, second = uuid.uuid4(), uuid.uuid4()
        network.set_upload_label(first, "first.bin")
        network.set_upload_label(second, "second.bin")
        assert network.upload_label(first) is None
        assert network.upload_label(second) == "second.bin"

    def test_packet_attempts_evict_the_oldest_past_the_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        network.reset_packets()
        monkeypatch.setattr(network, "_PACKET_ATTEMPT_CAP", 1)
        one, two = uuid.uuid4(), uuid.uuid4()
        network.packet_begin(
            network.PacketContext(
                "contact_read",
                stream_id=one,
                box_index=0,
            )
        )
        network.packet_begin(
            network.PacketContext(
                "contact_read",
                stream_id=two,
                box_index=0,
            )
        )
        again = network.PacketContext(
            "contact_read", stream_id=one, box_index=0
        )
        packet_id = network.packet_begin(again)
        record = next(
            p for p in network.packets_snapshot() if p["id"] == packet_id
        )
        assert record["attempt"] == 1

    def test_finishing_an_unknown_packet_is_a_no_op(self) -> None:
        network.reset_packets()
        network.packet_finish(None, network.PACKET_STATUS_ACKED)
        network.packet_finish("no-such-id", network.PACKET_STATUS_ACKED)
        assert network.packets_snapshot() == []

    @pytest.mark.asyncio
    async def test_an_unexpected_send_failure_is_recorded_as_error(
        self,
        fake_thinclient: _Fake,
    ) -> None:
        network.reset_packets()
        network.install_stats_counters(
            cast("ThinClient", fake_thinclient),
        )
        fake_thinclient.inject_error(
            "start_resending_encrypted_message",
            ValueError("bad envelope"),
        )
        context = network.PacketContext(
            "contact_read",
            stream_id=uuid.uuid4(),
            box_index=0,
        )
        with pytest.raises(ValueError):
            await fake_thinclient.start_resending_encrypted_message(
                read_cap=bytes(136),
                envelope_hash=b"eh",
                _packet_context=context,
            )
        record = next(
            p
            for p in network.packets_snapshot()
            if p["id"] == context.packet_id
        )
        assert record["status"] == network.PACKET_STATUS_ERROR


class TestPkiEpochEvents:
    @pytest.mark.asyncio
    async def test_an_unparsable_event_leaves_the_epoch_alone(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = network._epoch_event
        with caplog.at_level(logging.DEBUG, logger="katzen.network"):
            await network.on_new_pki_document({})
        assert network._epoch_event is before
        assert not before.is_set()
        assert any(
            "could not parse event payload" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_document_without_an_epoch_is_ignored(self) -> None:
        before = network._epoch_event
        await network.on_new_pki_document({"payload": cbor2.dumps({})})
        assert network._epoch_event is before
        assert not before.is_set()

    @pytest.mark.asyncio
    async def test_a_repeated_epoch_is_ignored(self) -> None:
        await network.on_new_pki_document(
            {"payload": cbor2.dumps({"Epoch": 7})}
        )
        rolled = network._epoch_event
        await network.on_new_pki_document(
            {"payload": cbor2.dumps({"Epoch": 7})}
        )
        assert network._epoch_event is rolled
        assert not rolled.is_set()


class TestCancelAndJoin:
    @pytest.mark.asyncio
    async def test_a_cancel_during_the_join_is_re_raised_after_joining(
        self,
    ) -> None:
        parked = asyncio.Event()
        released = asyncio.Event()
        absorbed: list[str] = []

        async def stubborn() -> None:
            try:
                await parked.wait()
            except asyncio.CancelledError:
                absorbed.append("absorbed")
                await released.wait()

        inner = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        joiner = asyncio.ensure_future(network._cancel_and_join((inner,)))
        await asyncio.sleep(0)
        joiner.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await joiner
        assert absorbed == ["absorbed"]
        assert inner.done() and not inner.cancelled()


class TestStartBackgroundThreads:
    @pytest.mark.asyncio
    async def test_a_worker_returning_early_is_reported_as_critical(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def _returns(
            worker: object,
            connection: object,
            *,
            on_restart: object = None,
        ) -> None:
            return None

        monkeypatch.setattr(network, "_supervised", _returns)
        await network.on_connection_status(
            {"is_connected": True, "err": None}
        )
        with caplog.at_level(logging.CRITICAL, logger="katzen.network"):
            await network.start_background_threads(_conn(fake_thinclient))
        assert any(
            "without a shutdown request" in r.message for r in caplog.records
        )


class TestRemint:
    @pytest.mark.asyncio
    async def test_reminting_a_deleted_row_reports_failure(self) -> None:
        mw = persistent.MixWAL(
            id=uuid.uuid4(),
            bacap_stream=uuid.uuid4(),
            envelope_hash=b"gone",
            encrypted_payload=b"ct",
            envelope_descriptor=b"ed",
            current_message_index=_idx(0),
            next_message_index=_idx(1),
            is_read=False,
        )
        fresh = SimpleNamespace(
            envelope_hash=b"new",
            message_ciphertext=b"ct2",
            envelope_descriptor=b"ed2",
            next_message_box_index=_idx(1),
        )
        assert await network._remint_mixwal(mw, fresh) is False

    @pytest.mark.asyncio
    async def test_a_missing_plaintext_drops_the_write_row(
        self,
        fake_thinclient: _Fake,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stream = uuid.uuid4()
        mw_id = await _add_mixwal(stream, is_read=False)
        detached = persistent.MixWAL(
            id=mw_id,
            bacap_stream=stream,
            plaintextwal=uuid.uuid4(),
            envelope_hash=b"x",
            encrypted_payload=b"ct",
            envelope_descriptor=b"ed",
            current_message_index=_idx(0),
            next_message_index=_idx(1),
            is_read=False,
        )
        wcw = persistent.WriteCapWAL(id=stream)
        with caplog.at_level(logging.CRITICAL, logger="katzen.network"):
            ok = await network._remint_write_envelope(
                _conn(fake_thinclient),
                detached,
                wcw,
            )
        assert ok is False
        assert any(
            "dropping the MixWAL row" in r.message for r in caplog.records
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, mw_id) is None


class TestPersistFirstUnread:
    @pytest.mark.asyncio
    async def test_a_missing_conversation_is_skipped(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await network.persist_first_unread(4242, 7)
        assert any("not found; skipping" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_cursor_is_persisted(self) -> None:
        conv_id, _peer_id, _stream = await _make_conversation()
        await network.persist_first_unread(conv_id, 3)
        async with persistent.asession() as sess:
            row = await sess.get(persistent.Conversation, conv_id)
            assert row is not None
            assert row.first_unread == 3


class TestTryAssemble:
    @pytest.mark.asyncio
    async def test_an_unterminated_chain_stays_open(self) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReceivedPiece(
                    read_cap=stream,
                    bacap_index=_idx(0)[:8],
                    chunk_type=b"C",
                    chunk=b"half",
                )
            )
            await sess.commit()
        async with persistent.asession() as sess:
            assert (
                await network._try_assemble(sess, stream, _idx(0)[:8]) is None
            )
            assert (
                await network._try_assemble(sess, stream, _idx(9)[:8]) is None
            )

    @pytest.mark.asyncio
    async def test_the_walk_back_stops_at_the_previous_message(self) -> None:
        stream = uuid.uuid4()
        gcm = models.GroupChatMessage(
            version=0,
            text="split",
        )
        blob = gcm.to_cbor()
        async with persistent.asession() as sess:
            sess.add_all(
                [
                    persistent.ReceivedPiece(
                        read_cap=stream,
                        bacap_index=_idx(0)[:8],
                        chunk_type=b"F",
                        chunk=b"older message",
                    ),
                    persistent.ReceivedPiece(
                        read_cap=stream,
                        bacap_index=_idx(1)[:8],
                        chunk_type=b"C",
                        chunk=blob[:4],
                    ),
                    persistent.ReceivedPiece(
                        read_cap=stream,
                        bacap_index=_idx(2)[:8],
                        chunk_type=b"F",
                        chunk=blob[4:],
                    ),
                ]
            )
            await sess.commit()
        async with persistent.asession() as sess:
            assembled = await network._try_assemble(sess, stream, _idx(2)[:8])
        assert assembled is not None
        assert assembled[0] == "F"
        kind, chunks, chain, parsed = cast(
            "tuple[str, list[tuple[bytes, bytes]], "
            "list[persistent.ReceivedPiece], models.GroupChatMessage]",
            assembled,
        )
        assert kind == "F"
        assert len(chain) == 2
        assert [rp.chunk_type for rp in chain] == [b"C", b"F"]
        assert parsed.text == "split"

    @pytest.mark.asyncio
    async def test_an_undecodable_chain_is_left_for_retry(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReceivedPiece(
                    read_cap=stream,
                    bacap_index=_idx(0)[:8],
                    chunk_type=b"F",
                    chunk=b"\xff\xff not cbor",
                )
            )
            await sess.commit()
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            async with persistent.asession() as sess:
                assembled = await network._try_assemble(
                    sess,
                    stream,
                    _idx(0)[:8],
                )
        assert assembled is None
        assert any(
            "could not assemble chain" in r.message for r in caplog.records
        )
        async with persistent.asession() as sess:
            assert (
                await network._get_received_piece(
                    sess,
                    stream,
                    _idx(0)[:8],
                )
                is not None
            )

    @pytest.mark.asyncio
    async def test_a_chain_that_decodes_to_nothing_is_not_assembled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReceivedPiece(
                    read_cap=stream,
                    bacap_index=_idx(0)[:8],
                    chunk_type=b"F",
                    chunk=b"whatever",
                )
            )
            await sess.commit()
        monkeypatch.setattr(
            models,
            "unserialize",
            ignore,
        )
        async with persistent.asession() as sess:
            assert (
                await network._try_assemble(sess, stream, _idx(0)[:8]) is None
            )


class TestSubstreamMiss:
    @pytest.mark.asyncio
    async def test_a_miss_on_an_unknown_stream_is_not_a_failure(self) -> None:
        assert (
            await network._record_substream_miss(
                uuid.uuid4(),
                terminal=True,
                now_s=100.0,
                budget_s=1.0,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_a_tombstone_retires_the_peer_and_its_pending_reads(
        self,
    ) -> None:
        conv_id, parent_id, _stream = await _make_conversation()
        sub = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=sub,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                )
            )
            sess.add(
                persistent.ConversationPeer(
                    name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:aa",
                    read_cap_id=sub,
                )
            )
            await sess.commit()
        mw_id = await _add_mixwal(sub)
        failed = await network._record_substream_miss(
            sub,
            terminal=True,
            now_s=100.0,
            budget_s=1200.0,
        )
        assert failed is True
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, sub)
            assert rcw is not None
            assert rcw.substream_failure == "A required box is tombstoned"
            assert await sess.get(persistent.MixWAL, mw_id) is None
            peers = (
                await sess.exec(
                    select(persistent.ConversationPeer).where(
                        persistent.ConversationPeer.read_cap_id == sub,
                    )
                )
            ).all()
            assert [p.active for p in peers] == [False]
        assert network.substream_progress_queue.get_nowait() == (
            transfer_events.failed(sub, "A required box is tombstoned")
        )

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_retires_the_transfer(self) -> None:
        sub = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=sub,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_missing_since=10.0,
                )
            )
            await sess.commit()
        failed = await network._record_substream_miss(
            sub,
            terminal=False,
            now_s=1000.0,
            budget_s=5.0,
        )
        assert failed is True
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, sub)
            assert rcw is not None
            assert (
                rcw.substream_failure == "A required box remained unavailable"
            )
        assert network.substream_progress_queue.get_nowait() == (
            transfer_events.failed(sub, "A required box remained unavailable")
        )


class TestPausePeerReads:
    @pytest.mark.asyncio
    async def test_pausing_an_unknown_stream_is_a_no_op(self) -> None:
        await network.pause_peer_reads(bacap_stream=uuid.uuid4())
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_an_inactive_peer_is_not_paused(self) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=stream,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                )
            )
            sess.add(
                persistent.ConversationPeer(
                    name="dormant",
                    read_cap_id=stream,
                    active=False,
                )
            )
            await sess.commit()
        await network.pause_peer_reads(bacap_stream=stream)
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            assert rcw.paused is False

    @pytest.mark.asyncio
    async def test_a_failing_reader_is_logged_and_the_pause_completes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=stream,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                )
            )
            sess.add(
                persistent.ConversationPeer(name="p", read_cap_id=stream)
            )
            await sess.commit()
        mw_id = await _add_mixwal(stream)

        async def explodes() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("reader blew up") from None

        reader = asyncio.ensure_future(explodes())
        await asyncio.sleep(0)
        network._inflight_reads[stream] = reader
        try:
            with caplog.at_level(logging.ERROR, logger="katzen.network"):
                await network.pause_peer_reads(bacap_stream=stream)
        finally:
            network._inflight_reads.pop(stream, None)
        assert any(
            "Read failed while pausing" in r.message for r in caplog.records
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            assert rcw.paused is True
            assert await sess.get(persistent.MixWAL, mw_id) is None

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_propagates_its_own_cancellation(
        self,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=stream,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                )
            )
            sess.add(
                persistent.ConversationPeer(name="p", read_cap_id=stream)
            )
            await sess.commit()
        entered = asyncio.Event()
        released = asyncio.Event()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set()
                await released.wait()

        reader = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        network._inflight_reads[stream] = reader
        pauser = asyncio.ensure_future(
            network.pause_peer_reads(bacap_stream=stream),
        )
        try:
            await entered.wait()
            pauser.cancel()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await pauser
            assert pauser.cancelled()
        finally:
            network._inflight_reads.pop(stream, None)
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            assert rcw.paused is True


class TestResumePeerReads:
    @pytest.mark.asyncio
    async def test_resuming_an_unknown_stream_is_a_no_op(self) -> None:
        await network.resume_peer_reads(bacap_stream=uuid.uuid4())
        assert network.substream_progress_queue.empty()


class TestUploadControls:
    @pytest.mark.asyncio
    async def test_an_unknown_read_cap_has_no_upload_stream(self) -> None:
        assert await network._upload_stream_for_rcw(uuid.uuid4()) is None
        await network.pause_upload(rcw_id=uuid.uuid4())
        await network.cancel_upload(rcw_id=uuid.uuid4())
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_a_main_stream_read_cap_is_not_an_upload(self) -> None:
        rcw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=rcw_id))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=rcw_id,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                )
            )
            await sess.commit()
        assert await network._upload_stream_for_rcw(rcw_id) is None
        await network.pause_upload(rcw_id=rcw_id)
        await network.resume_upload(rcw_id=rcw_id)
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_a_failing_writer_is_logged_and_the_pause_completes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            await sess.commit()

        async def explodes() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("writer blew up") from None

        writer = asyncio.ensure_future(explodes())
        await asyncio.sleep(0)
        network._inflight_writes[agg] = writer
        try:
            with caplog.at_level(logging.ERROR, logger="katzen.network"):
                await network.pause_upload(rcw_id=rcw_id)
        finally:
            network._inflight_writes.pop(agg, None)
        assert any(
            "write drain failed while pausing" in r.message
            for r in caplog.records
        )
        async with persistent.asession() as sess:
            wcw = await sess.get(persistent.WriteCapWAL, agg)
            assert wcw is not None
            assert wcw.paused is True
        assert network.substream_progress_queue.get_nowait() == (
            transfer_events.paused(rcw_id, "upload", True)
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_propagates_its_own_cancellation(
        self,
    ) -> None:
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            await sess.commit()
        entered = asyncio.Event()
        released = asyncio.Event()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set()
                await released.wait()

        writer = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        network._inflight_writes[agg] = writer
        pauser = asyncio.ensure_future(network.pause_upload(rcw_id=rcw_id))
        try:
            await entered.wait()
            pauser.cancel()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await pauser
            assert pauser.cancelled()
        finally:
            network._inflight_writes.pop(agg, None)
        assert network.substream_progress_queue.empty()


class TestDismissFailedTransfer:
    @pytest.mark.asyncio
    async def test_dismissing_an_unknown_stream_is_a_no_op(self) -> None:
        await network.dismiss_failed_transfer(bacap_stream=uuid.uuid4())
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_dismissing_without_a_failure_is_idempotent(self) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=stream,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    paused=True,
                )
            )
            await sess.commit()
        await network.dismiss_failed_transfer(bacap_stream=stream)
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            assert rcw.paused is True

    @pytest.mark.asyncio
    async def test_a_contact_stream_cannot_be_dismissed(self) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=stream,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_failure="boom",
                )
            )
            sess.add(
                persistent.ConversationPeer(
                    name="contact",
                    read_cap_id=stream,
                )
            )
            await sess.commit()
        with pytest.raises(ValueError, match="not a transfer"):
            await network.dismiss_failed_transfer(bacap_stream=stream)

    @pytest.mark.asyncio
    async def test_dismissing_clears_pieces_and_pending_reads(self) -> None:
        _conv_id, parent_id, _stream = await _make_conversation()
        sub = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.ReadCapWAL(
                    id=sub,
                    write_cap_id=None,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_failure="boom",
                    paused=True,
                )
            )
            sess.add(
                persistent.ConversationPeer(
                    name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:bb",
                    read_cap_id=sub,
                )
            )
            sess.add(
                persistent.ReceivedPiece(
                    read_cap=sub,
                    bacap_index=_idx(0)[:8],
                    chunk_type=b"C",
                    chunk=b"partial",
                )
            )
            await sess.commit()
        mw_id = await _add_mixwal(sub)
        await network.dismiss_failed_transfer(bacap_stream=sub)
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, sub)
            assert rcw is not None
            assert rcw.substream_failure is None
            assert rcw.paused is False
            assert await sess.get(persistent.MixWAL, mw_id) is None
            assert (
                await network._get_received_piece(
                    sess,
                    sub,
                    _idx(0)[:8],
                )
                is None
            )


class TestResolveThinclientConfig:
    def test_a_missing_bundled_copy_falls_back_to_the_repo_tree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        def _no_package(name: str) -> object:
            raise ModuleNotFoundError(name)

        monkeypatch.delenv("KATZENQT_THINCLIENT_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(network.importlib.resources, "files", _no_package)
        try:
            resolved = network.resolve_thinclient_config()
        except ModuleNotFoundError:
            pytest.skip("this tree reads the config as package data only")
        assert resolved.name == "thinclient.toml"

    def test_nothing_found_anywhere_is_an_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        monkeypatch.delenv("KATZENQT_THINCLIENT_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(network.Path, "is_file", returning(False))
        with pytest.raises(FileNotFoundError, match="Could not locate"):
            network.resolve_thinclient_config()


class TestCancelUpload:
    @pytest.mark.asyncio
    async def test_an_indirection_without_a_read_cap_row_is_left_alone(
        self,
    ) -> None:
        conv_id, _peer_id, main = await _make_conversation()
        rcw_id = uuid.uuid4()
        i_chunk_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(
                persistent.PlaintextWAL(
                    id=i_chunk_id,
                    bacap_stream=main,
                    conversation_id=conv_id,
                    bacap_payload=b"I",
                    indirection=rcw_id,
                )
            )
            await sess.commit()
        await network.cancel_upload(rcw_id=rcw_id)
        async with persistent.asession() as sess:
            assert (
                await sess.get(persistent.PlaintextWAL, i_chunk_id)
                is not None
            )
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_a_finished_upload_is_no_longer_cancellable(self) -> None:
        conv_id, _peer_id, main = await _make_conversation()
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        i_chunk_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=i_chunk_id,
                    bacap_stream=main,
                    conversation_id=conv_id,
                    bacap_payload=b"I",
                    indirection=rcw_id,
                )
            )
            await sess.commit()
        await network.cancel_upload(rcw_id=rcw_id)
        async with persistent.asession() as sess:
            assert (
                await sess.get(persistent.PlaintextWAL, i_chunk_id)
                is not None
            )
            assert await sess.get(persistent.ReadCapWAL, rcw_id) is not None
            assert await sess.get(persistent.WriteCapWAL, agg) is not None
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_an_acknowledged_chunk_finishes_before_the_cancel(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conv_id, _peer_id, main = await _make_conversation()
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        i_chunk_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=i_chunk_id,
                    bacap_stream=main,
                    conversation_id=conv_id,
                    bacap_payload=b"I",
                    indirection=rcw_id,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=uuid.uuid4(),
                    bacap_stream=agg,
                    conversation_id=conv_id,
                    bacap_payload=b"Cchunk",
                )
            )
            await sess.commit()
        real = persistent.asession
        opened: list[int] = []
        acked = asyncio.Event()

        @contextlib.asynccontextmanager
        async def _wrapped() -> AsyncIterator[persistent.AsyncSession]:
            nth = len(opened)
            opened.append(nth)
            async with real() as sess:
                yield sess
            if nth == 0:
                acked.set()

        async def _finishing_write() -> None:
            await acked.wait()
            async with real() as sess:
                row = await sess.get(persistent.PlaintextWAL, i_chunk_id)
                if row is not None:
                    await sess.delete(row)
                    await sess.commit()
            raise RuntimeError("ack bookkeeping blew up")

        writer = asyncio.ensure_future(_finishing_write())
        network._inflight_writes[agg] = writer
        network._write_acknowledged.add(agg)
        monkeypatch.setattr(persistent, "asession", _wrapped)
        try:
            with caplog.at_level(logging.ERROR, logger="katzen.network"):
                await network.cancel_upload(rcw_id=rcw_id)
        finally:
            monkeypatch.setattr(persistent, "asession", real)
            network._inflight_writes.pop(agg, None)
            network._write_acknowledged.discard(agg)
        assert any(
            "write drain failed during cancel" in r.message
            for r in caplog.records
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.PlaintextWAL, i_chunk_id) is None
            assert await sess.get(persistent.WriteCapWAL, agg) is not None
        assert network.substream_progress_queue.empty()

    @pytest.mark.asyncio
    async def test_a_failing_writer_is_logged_and_the_cancel_completes(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conv_id, _peer_id, main = await _make_conversation()
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        i_chunk_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=i_chunk_id,
                    bacap_stream=main,
                    conversation_id=conv_id,
                    bacap_payload=b"I",
                    indirection=rcw_id,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=uuid.uuid4(),
                    bacap_stream=agg,
                    conversation_id=conv_id,
                    bacap_payload=b"Cchunk",
                )
            )
            await sess.commit()

        async def explodes() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("writer blew up") from None

        writer = asyncio.ensure_future(explodes())
        await asyncio.sleep(0)
        network._inflight_writes[agg] = writer
        try:
            with caplog.at_level(logging.ERROR, logger="katzen.network"):
                await network.cancel_upload(rcw_id=rcw_id)
        finally:
            network._inflight_writes.pop(agg, None)
        assert any(
            "write drain failed during cancel" in r.message
            for r in caplog.records
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.PlaintextWAL, i_chunk_id) is None
            assert await sess.get(persistent.ReadCapWAL, rcw_id) is None
            assert await sess.get(persistent.WriteCapWAL, agg) is None
        assert network.substream_progress_queue.get_nowait() == (
            transfer_events.completed(rcw_id, "upload", cancelled=True)
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_propagates_its_own_cancellation(
        self,
    ) -> None:
        conv_id, _peer_id, main = await _make_conversation()
        agg = uuid.uuid4()
        rcw_id = uuid.uuid4()
        i_chunk_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=agg))
            sess.add(
                persistent.ReadCapWAL(
                    id=rcw_id,
                    write_cap_id=agg,
                    read_cap=bytes(136),
                    next_index=_idx(0),
                    substream_total_chunks=2,
                )
            )
            sess.add(
                persistent.PlaintextWAL(
                    id=i_chunk_id,
                    bacap_stream=main,
                    conversation_id=conv_id,
                    bacap_payload=b"I",
                    indirection=rcw_id,
                )
            )
            await sess.commit()
        entered = asyncio.Event()
        released = asyncio.Event()

        async def stubborn() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set()
                await released.wait()

        writer = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        network._inflight_writes[agg] = writer
        canceller = asyncio.ensure_future(
            network.cancel_upload(rcw_id=rcw_id)
        )
        try:
            await entered.wait()
            canceller.cancel()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await canceller
            assert canceller.cancelled()
        finally:
            network._inflight_writes.pop(agg, None)
        async with persistent.asession() as sess:
            assert (
                await sess.get(persistent.PlaintextWAL, i_chunk_id)
                is not None
            )


class TestConsensusSummary:
    def test_an_unknown_epoch_period_leaves_the_age_unknown(self) -> None:
        summary = network.summarize_pki_document({"Epoch": 0})
        assert summary is not None
        assert summary.period_seconds is None
        assert summary.consensus_seconds is None
        assert summary.epochs_elapsed == 0
