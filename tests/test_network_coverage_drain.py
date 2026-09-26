from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import NamedTuple, Protocol, cast

import pytest
from katzenpost_thinclient.pigeonhole import (
    EncryptReadResult,
    EncryptWriteResult,
    KeypairResult,
)
from pycrdt import Doc
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from katzenqt import conversation_handlers, models, network, persistent
from katzenqt.tally import events, sync
from katzenqt.tally.controller import INSTANCE as TALLY
from katzenqt.tally.schema import Mode


class _Fake(Protocol):
    async def new_keypair(self, seed: bytes) -> KeypairResult: ...

    async def encrypt_write(
        self,
        plaintext: bytes,
        write_cap: bytes,
        message_box_index: bytes,
    ) -> EncryptWriteResult: ...

    async def encrypt_read(
        self,
        read_cap: bytes,
        message_box_index: bytes,
    ) -> EncryptReadResult: ...

    def pre_store(
        self,
        *,
        write_cap: bytes,
        message_box_index: bytes,
        plaintext: bytes,
    ) -> None: ...

    def hold_ack_for_box(
        self,
        box_id: bytes,
        message_box_index: bytes,
    ) -> None: ...

    def call_count(self, method: str) -> int: ...


def _conn(fake: _Fake) -> network.ThinClient:
    return cast(network.ThinClient, fake)


def _busy_error() -> OperationalError:
    return OperationalError("stmt", {}, Exception("database is locked"))


def _fatal_db_error() -> OperationalError:
    return OperationalError("stmt", {}, Exception("no such table: mixwal"))


def _f_payload(text: str = "hello") -> bytes:
    gcm = models.GroupChatMessage(
        version=0,
        membership_hash=b"X" * 32,
        text=text,
    )
    return b"F" + bytes(gcm.to_cbor())


class _Flow(NamedTuple):
    stream: uuid.UUID
    write_cap: bytes
    read_cap: bytes
    first_index: bytes
    next_index: bytes
    conversation_id: int
    peer_id: int
    mw_id: uuid.UUID
    pwal_id: uuid.UUID


async def _base_rows(
    fake: _Fake,
    *,
    peer_name: str,
    active: bool,
    seed: bytes,
) -> tuple[KeypairResult, uuid.UUID, int, int]:
    kp = await fake.new_keypair(seed)
    stream = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.WriteCapWAL(
                id=stream,
                write_cap=kp.write_cap,
                next_index=kp.first_message_index,
            )
        )
        sess.add(
            persistent.ReadCapWAL(
                id=stream,
                write_cap_id=stream,
                read_cap=kp.read_cap,
                next_index=kp.first_message_index,
            )
        )
        peer = persistent.ConversationPeer(
            name=peer_name,
            read_cap_id=stream,
            active=active,
        )
        sess.add(peer)
        await sess.commit()
        await sess.refresh(peer)
        peer_id = int(peer.id)
        conv = persistent.Conversation(
            name="demo",
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
    return kp, stream, conv_id, peer_id


async def _write_flow(
    fake: _Fake,
    *,
    payload: bytes = b"Fhello",
    seed: bytes = b"\x33" * 32,
) -> _Flow:
    kp, stream, conv_id, peer_id = await _base_rows(
        fake,
        peer_name="self",
        active=False,
        seed=seed,
    )
    wcr = await fake.encrypt_write(
        plaintext=payload,
        write_cap=kp.write_cap,
        message_box_index=kp.first_message_index,
    )
    pwal_id, mw_id = uuid.uuid4(), uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.PlaintextWAL(
                id=pwal_id,
                bacap_stream=stream,
                conversation_id=conv_id,
                bacap_payload=payload,
            )
        )
        sess.add(
            persistent.MixWAL(
                id=mw_id,
                plaintextwal=pwal_id,
                bacap_stream=stream,
                envelope_hash=wcr.envelope_hash,
                encrypted_payload=wcr.message_ciphertext,
                envelope_descriptor=wcr.envelope_descriptor,
                current_message_index=kp.first_message_index,
                next_message_index=wcr.next_message_box_index,
                is_read=False,
            )
        )
        await sess.commit()
    return _Flow(
        stream=stream,
        write_cap=kp.write_cap,
        read_cap=kp.read_cap,
        first_index=kp.first_message_index,
        next_index=wcr.next_message_box_index,
        conversation_id=conv_id,
        peer_id=peer_id,
        mw_id=mw_id,
        pwal_id=pwal_id,
    )


async def _read_flow(
    fake: _Fake,
    *,
    payload: bytes,
    peer_name: str = "peer",
    seed: bytes = b"\x44" * 32,
) -> _Flow:
    kp, stream, conv_id, peer_id = await _base_rows(
        fake,
        peer_name=peer_name,
        active=True,
        seed=seed,
    )
    fake.pre_store(
        write_cap=kp.write_cap,
        message_box_index=kp.first_message_index,
        plaintext=payload,
    )
    rcr = await fake.encrypt_read(
        read_cap=kp.read_cap,
        message_box_index=kp.first_message_index,
    )
    mw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(
            persistent.MixWAL(
                id=mw_id,
                plaintextwal=None,
                bacap_stream=stream,
                envelope_hash=rcr.envelope_hash,
                encrypted_payload=rcr.message_ciphertext,
                envelope_descriptor=rcr.envelope_descriptor,
                current_message_index=kp.first_message_index,
                next_message_index=rcr.next_message_box_index,
                is_read=True,
            )
        )
        await sess.commit()
    return _Flow(
        stream=stream,
        write_cap=kp.write_cap,
        read_cap=kp.read_cap,
        first_index=kp.first_message_index,
        next_index=rcr.next_message_box_index,
        conversation_id=conv_id,
        peer_id=peer_id,
        mw_id=mw_id,
        pwal_id=uuid.uuid4(),
    )


async def _load_mw(mw_id: uuid.UUID) -> persistent.MixWAL:
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, mw_id)
        assert mw is not None
        return mw


def _drain_progress() -> list[tuple[object, ...]]:
    events_seen: list[tuple[object, ...]] = []
    while not network.substream_progress_queue.empty():
        events_seen.append(network.substream_progress_queue.get_nowait())
    return events_seen


@pytest.fixture(autouse=True)
def _drain_conversation_updates() -> Iterator[None]:
    yield
    while not network.conversation_update_queue.empty():
        network.conversation_update_queue.get_nowait()


class TestWriteAckBookkeeping:
    @pytest.mark.asyncio
    async def test_a_fatal_database_error_is_not_swallowed(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = await _write_flow(fake_thinclient)

        async def _boom(
            connection: object,
            mw: object,
            queue: object,
            **kwargs: object,
        ) -> int:
            raise _fatal_db_error()

        monkeypatch.setattr(persistent.SentLog, "mark_sent", _boom)
        mw = await _load_mw(flow.mw_id)
        with pytest.raises(OperationalError):
            await network.drain_mixwal_write_single(
                _conn(fake_thinclient),
                mw,
                {flow.stream},
            )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None

    @pytest.mark.asyncio
    async def test_a_cancel_waits_for_the_bookkeeping_to_finish(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        flow = await _write_flow(fake_thinclient)
        entered = asyncio.Event()
        released = asyncio.Event()

        async def _slow(
            connection: object,
            mw: object,
            queue: object,
            **kwargs: object,
        ) -> int:
            entered.set()
            await released.wait()
            raise RuntimeError("bookkeeping blew up")

        monkeypatch.setattr(persistent.SentLog, "mark_sent", _slow)
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        task = asyncio.ensure_future(
            network.drain_mixwal_write_single(
                _conn(fake_thinclient),
                mw,
                draining,
            )
        )
        with caplog.at_level(logging.ERROR, logger="katzen.network"):
            await entered.wait()
            task.cancel()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert any(
            "ACK bookkeeping failed after cancellation" in r.message
            for r in caplog.records
        )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None


class TestUploadProgressAfterAck:
    @pytest.mark.asyncio
    async def test_sqlite_busy_only_costs_the_progress_event(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        flow = await _write_flow(fake_thinclient)

        async def _busy(bacap_stream: uuid.UUID) -> None:
            raise _busy_error()

        monkeypatch.setattr(persistent, "upload_progress_after_ack", _busy)
        mw = await _load_mw(flow.mw_id)
        _drain_progress()
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await network.drain_mixwal_write_single(
                _conn(fake_thinclient),
                mw,
                {flow.stream},
            )
        assert any(
            "sqlite busy reading upload progress" in r.message
            for r in caplog.records
        )
        assert _drain_progress() == []
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is None
            assert (
                len((await sess.exec(select(persistent.SentLog))).all()) == 1
            )

    @pytest.mark.asyncio
    async def test_a_fatal_database_error_is_not_swallowed(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = await _write_flow(fake_thinclient)

        async def _fatal(bacap_stream: uuid.UUID) -> None:
            raise _fatal_db_error()

        monkeypatch.setattr(persistent, "upload_progress_after_ack", _fatal)
        mw = await _load_mw(flow.mw_id)
        with pytest.raises(OperationalError):
            await network.drain_mixwal_write_single(
                _conn(fake_thinclient),
                mw,
                {flow.stream},
            )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is None


class TestReadGiveUpPaths:
    @pytest.mark.asyncio
    async def test_a_paused_stream_is_not_cast(
        self,
        fake_thinclient: _Fake,
    ) -> None:
        flow = await _read_flow(fake_thinclient, payload=_f_payload())
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            rcw.paused = True
            sess.add(rcw)
            await sess.commit()
        before = fake_thinclient.call_count("encrypt_read")
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        await network.drain_mixwal_read_single(
            connection=_conn(fake_thinclient),
            rcw_read_cap=flow.read_cap,
            mw=mw,
            draining_right_now=draining,
        )
        assert draining == set()
        assert fake_thinclient.call_count("encrypt_read") == before
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None

    @pytest.mark.asyncio
    async def test_a_connection_life_signal_reschedules_the_read(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        flow = await _read_flow(fake_thinclient, payload=_f_payload())

        async def _interrupted(
            connection: object,
            **kwargs: object,
        ) -> object:
            raise network.ConnectionLifeInterruptedError(
                "reconnect mid-read",
                reason="reconnect",
            )

        monkeypatch.setattr(network, "_await_read_reply", _interrupted)
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await network.drain_mixwal_read_single(
                connection=_conn(fake_thinclient),
                rcw_read_cap=flow.read_cap,
                mw=mw,
                draining_right_now=draining,
            )
        assert any(
            "re-scheduling the read" in r.message for r in caplog.records
        )
        assert draining == set()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            assert rcw.next_index == flow.first_index

    @pytest.mark.asyncio
    async def test_an_unanswered_cancel_is_logged_and_the_read_retried(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        flow = await _read_flow(fake_thinclient, payload=_f_payload())
        fake_thinclient.hold_ack_for_box(flow.read_cap, flow.first_index)

        async def _never_answers(envelope_hash: bytes) -> None:
            raise asyncio.TimeoutError()

        monkeypatch.setattr(
            fake_thinclient,
            "cancel_resending_encrypted_message",
            _never_answers,
        )
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await network.drain_mixwal_read_single(
                connection=_conn(fake_thinclient),
                rcw_read_cap=flow.read_cap,
                mw=mw,
                draining_right_now=draining,
                read_watchdog_s=0.05,
            )
        assert any(
            "cancel ARQ did not answer" in r.message for r in caplog.records
        )
        assert draining == set()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            assert rcw.next_index == flow.first_index


class TestReadSubstreamFailures:
    @pytest.mark.asyncio
    async def test_an_invalid_chunk_prefix_fails_the_transfer(
        self,
        fake_thinclient: _Fake,
    ) -> None:
        flow = await _read_flow(
            fake_thinclient,
            payload=b"Zbogus",
            peer_name=":substream:77:aa",
        )
        _drain_progress()
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        await network.drain_mixwal_read_single(
            connection=_conn(fake_thinclient),
            rcw_read_cap=flow.read_cap,
            mw=mw,
            draining_right_now=draining,
        )
        assert draining == set()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is None
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            assert rcw.substream_failure == (
                "The transfer contains an invalid chunk prefix"
            )
            peer = await sess.get(persistent.ConversationPeer, flow.peer_id)
            assert peer is not None
            assert peer.active is False
            assert (
                await sess.exec(select(persistent.ReceivedPiece))
            ).all() == []
        assert _drain_progress() == [
            (
                "failed",
                flow.stream,
                "The transfer contains an invalid chunk prefix",
            )
        ]

    @pytest.mark.asyncio
    async def test_a_substream_without_a_parent_is_retired(
        self,
        fake_thinclient: _Fake,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        flow = await _read_flow(
            fake_thinclient,
            payload=_f_payload("orphan"),
            peer_name=":substream:999999:zz",
        )
        _drain_progress()
        mw = await _load_mw(flow.mw_id)
        draining = {flow.stream}
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await network.drain_mixwal_read_single(
                connection=_conn(fake_thinclient),
                rcw_read_cap=flow.read_cap,
                mw=mw,
                draining_right_now=draining,
            )
        assert any(
            "retiring substream with no parent" in r.message
            for r in caplog.records
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            assert (
                rcw.substream_failure
                == "The transfer parent no longer exists"
            )
            peer = await sess.get(persistent.ConversationPeer, flow.peer_id)
            assert peer is not None
            assert peer.active is False
            assert await sess.get(persistent.MixWAL, flow.mw_id) is None
            assert (
                await sess.exec(select(persistent.ConversationLog))
            ).all() == []
        assert (
            "failed",
            flow.stream,
            "The transfer parent no longer exists",
        ) in (_drain_progress())

    @pytest.mark.asyncio
    async def test_a_fatal_database_error_on_commit_is_not_swallowed(
        self,
        fake_thinclient: _Fake,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = await _read_flow(fake_thinclient, payload=_f_payload())

        async def _boom(
            sess: object,
            peer: object,
            gcm: object,
            full_payload: object,
        ) -> tuple[bool, bool, None, bool]:
            raise _fatal_db_error()

        monkeypatch.setattr(conversation_handlers, "dispatch", _boom)
        mw = await _load_mw(flow.mw_id)
        with pytest.raises(OperationalError):
            await network.drain_mixwal_read_single(
                connection=_conn(fake_thinclient),
                rcw_read_cap=flow.read_cap,
                mw=mw,
                draining_right_now={flow.stream},
            )
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, flow.mw_id) is not None
            rcw = await sess.get(persistent.ReadCapWAL, flow.stream)
            assert rcw is not None
            assert rcw.next_index == flow.first_index


class TestReadStagesOutboundWork:
    @pytest.mark.asyncio
    async def test_a_tally_sync_request_pokes_the_send_loop(
        self,
        fake_thinclient: _Fake,
    ) -> None:
        TALLY._docs.clear()
        survey_id = uuid.uuid4().bytes
        payload = (
            b"F"
            + events.build_sync_request(
                survey_id,
                sync.state_vector(Doc()),
            ).to_cbor()
        )
        flow = await _read_flow(fake_thinclient, payload=payload)
        async with persistent.asession() as sess:
            conv = await sess.get(
                persistent.Conversation, flow.conversation_id
            )
            assert conv is not None
            await TALLY.create_local(
                sess,
                conv,
                survey_id,
                "topic",
                Mode.APPROVAL,
                ["a"],
            )
            await sess.commit()
        network.resendable_event.clear()
        mw = await _load_mw(flow.mw_id)
        await network.drain_mixwal_read_single(
            connection=_conn(fake_thinclient),
            rcw_read_cap=flow.read_cap,
            mw=mw,
            draining_right_now={flow.stream},
        )
        assert network.resendable_event.is_set()
        async with persistent.asession() as sess:
            staged = (await sess.exec(select(persistent.PlaintextWAL))).all()
            assert len(staged) >= 1
