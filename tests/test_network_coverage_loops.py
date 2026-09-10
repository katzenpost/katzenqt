from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Protocol, cast

import pytest
from katzenpost_thinclient.pigeonhole import KeypairResult
from sqlalchemy.exc import OperationalError

from katzenqt import network, persistent
from functools import partial
from tests.stubs import is_clear, logged


class _Fake(Protocol):
    async def new_keypair(self, seed: bytes) -> KeypairResult: ...

    def call_count(self, method: str) -> int: ...


def _conn(fake: _Fake) -> network.ThinClient:
    return cast(network.ThinClient, fake)


def _event(name: str) -> asyncio.Event:
    return cast(asyncio.Event, getattr(network, name))


def _resend_queue() -> set[uuid.UUID]:
    return cast("set[uuid.UUID]", getattr(network, "__resend_queue"))


def _idx(counter: int) -> bytes:
    return counter.to_bytes(8, "little") + bytes(96)


async def _spin_until(check: Callable[[], bool], budget_s: float = 30.0) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never reached")


async def _add_read_mixwal(stream: uuid.UUID) -> uuid.UUID:
    mw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(persistent.MixWAL(
            id=mw_id, bacap_stream=stream, plaintextwal=None,
            envelope_hash=uuid.uuid4().bytes, encrypted_payload=b"ct",
            envelope_descriptor=b"ed", current_message_index=_idx(0),
            next_message_index=_idx(1), is_read=True,
        ))
        await sess.commit()
    return mw_id


async def _run_one_drain_pass(fake: _Fake) -> None:
    _event("__resend_queue_populated").set()
    updated = _event("__mixwal_updated")
    updated.set()
    task = asyncio.ensure_future(network.drain_mixwal2(_conn(fake)))
    try:
        await _spin_until(partial(is_clear, updated))
    finally:
        network.shutdown()
        await asyncio.wait_for(task, timeout=10.0)


@pytest.fixture(autouse=True)
def _drain_conversation_updates() -> Iterator[None]:
    yield
    while not network.conversation_update_queue.empty():
        network.conversation_update_queue.get_nowait()


class TestDrainMixwalReadSkips:
    @pytest.mark.asyncio
    async def test_a_read_without_a_read_cap_row_is_skipped(
        self, fake_thinclient: _Fake,
    ) -> None:
        stream = uuid.uuid4()
        mw_id = await _add_read_mixwal(stream)
        await _run_one_drain_pass(fake_thinclient)
        assert fake_thinclient.call_count("encrypt_read") == 0
        assert stream not in _resend_queue()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, mw_id) is not None

    @pytest.mark.asyncio
    async def test_a_paused_read_is_skipped(
        self, fake_thinclient: _Fake,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.ReadCapWAL(
                id=stream, write_cap_id=None, read_cap=bytes(136),
                next_index=_idx(0), paused=True,
            ))
            await sess.commit()
        mw_id = await _add_read_mixwal(stream)
        await _run_one_drain_pass(fake_thinclient)
        assert fake_thinclient.call_count("encrypt_read") == 0
        assert stream not in _resend_queue()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, mw_id) is not None

    @pytest.mark.asyncio
    async def test_a_malformed_read_cap_skips_only_that_stream(
        self, fake_thinclient: _Fake, caplog: pytest.LogCaptureFixture,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.ReadCapWAL(
                id=stream, write_cap_id=None, read_cap=b"too short",
                next_index=_idx(0),
            ))
            await sess.commit()
        mw_id = await _add_read_mixwal(stream)
        with caplog.at_level(logging.ERROR, logger="katzen.network"):
            await _run_one_drain_pass(fake_thinclient)
        assert any(
            "has incorrect length" in r.message for r in caplog.records
        )
        assert fake_thinclient.call_count("encrypt_read") == 0
        assert stream not in _resend_queue()
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, mw_id) is not None

    @pytest.mark.asyncio
    async def test_a_paused_write_keeps_its_row(
        self, fake_thinclient: _Fake,
    ) -> None:
        stream = uuid.uuid4()
        mw_id = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=stream, paused=True))
            sess.add(persistent.MixWAL(
                id=mw_id, bacap_stream=stream, plaintextwal=None,
                envelope_hash=uuid.uuid4().bytes, encrypted_payload=b"ct",
                envelope_descriptor=b"ed", current_message_index=_idx(0),
                next_message_index=_idx(1), is_read=False,
            ))
            await sess.commit()
        await network.on_connection_status({"is_connected": True, "err": None})
        await _run_one_drain_pass(fake_thinclient)
        assert fake_thinclient.call_count(
            "start_resending_encrypted_message",
        ) == 0
        async with persistent.asession() as sess:
            assert await sess.get(persistent.MixWAL, mw_id) is not None


class TestProvisionReadCaps:
    @pytest.mark.asyncio
    async def test_a_fresh_keypair_is_persisted_and_the_loops_are_poked(
        self, fake_thinclient: _Fake,
    ) -> None:
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(id=stream))
            sess.add(persistent.ReadCapWAL(id=stream, write_cap_id=stream))
            await sess.commit()
        network.resendable_event.clear()
        network.readables_to_mixwal_event.clear()
        task = asyncio.ensure_future(
            network.provision_read_caps(_conn(fake_thinclient)),
        )
        try:
            await _spin_until(network.resendable_event.is_set)
        finally:
            network.shutdown()
            await asyncio.wait_for(task, timeout=10.0)
        assert network.readables_to_mixwal_event.is_set()
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            wcw = await sess.get(persistent.WriteCapWAL, stream)
            assert rcw is not None and wcw is not None
            assert wcw.write_cap is not None
            assert rcw.read_cap == wcw.write_cap[32:]
            assert rcw.next_index == wcw.next_index


class TestReadablesToMixwalShutdown:
    @pytest.mark.asyncio
    async def test_a_shutdown_while_offline_ends_the_loop(
        self, fake_thinclient: _Fake,
    ) -> None:
        _event("__resend_queue_populated").set()
        task = asyncio.ensure_future(
            network.readables_to_mixwal(_conn(fake_thinclient)),
        )
        await asyncio.sleep(0)
        network.shutdown()
        await asyncio.wait_for(task, timeout=10.0)
        assert fake_thinclient.call_count("encrypt_read") == 0

    @pytest.mark.asyncio
    async def test_a_shutdown_while_armed_ends_the_loop(
        self, fake_thinclient: _Fake,
    ) -> None:
        kp = await fake_thinclient.new_keypair(b"\x55" * 32)
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.ReadCapWAL(
                id=stream, write_cap_id=None, read_cap=kp.read_cap,
                next_index=kp.first_message_index,
            ))
            sess.add(persistent.ConversationPeer(
                name="peer", read_cap_id=stream,
            ))
            await sess.commit()
        await network.on_connection_status({"is_connected": True, "err": None})
        _event("__resend_queue_populated").set()
        updated = _event("__mixwal_updated")
        updated.clear()
        task = asyncio.ensure_future(
            network.readables_to_mixwal(_conn(fake_thinclient)),
        )
        await _spin_until(updated.is_set)
        network.shutdown()
        network.readables_to_mixwal_event.set()
        await asyncio.wait_for(task, timeout=10.0)
        async with persistent.asession() as sess:
            armed = (await sess.exec(
                persistent.select(persistent.MixWAL),
            )).all()
        assert [row.bacap_stream for row in armed] == [stream]


class TestSendResendablePlaintexts:
    @pytest.mark.asyncio
    async def test_a_shutdown_while_idle_ends_the_loop(
        self, fake_thinclient: _Fake,
    ) -> None:
        await network.on_connection_status({"is_connected": True, "err": None})
        network.resendable_event.clear()
        task = asyncio.ensure_future(
            network.send_resendable_plaintexts(_conn(fake_thinclient)),
        )
        await _event("__resend_queue_populated").wait()
        network.shutdown()
        network.resendable_event.set()
        await asyncio.wait_for(task, timeout=10.0)
        assert fake_thinclient.call_count("encrypt_write") == 0

    @pytest.mark.asyncio
    async def test_sqlite_busy_defers_the_sweep(
        self, fake_thinclient: _Fake, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="katzen.network"):
            await self._sweep_with_failing_commit(
                fake_thinclient, monkeypatch,
                Exception("database is locked"), fatal=False,
            )
        assert any(
            "sqlite busy; retrying on the next sweep" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_a_fatal_database_error_is_not_swallowed(
        self, fake_thinclient: _Fake, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(OperationalError):
            await self._sweep_with_failing_commit(
                fake_thinclient, monkeypatch,
                Exception("no such table: plaintextwal"), fatal=True,
            )

    async def _sweep_with_failing_commit(
        self, fake: _Fake, monkeypatch: pytest.MonkeyPatch,
        orig: Exception, *, fatal: bool,
    ) -> None:
        real = persistent.asession
        failures: list[str] = []

        class _CommitFails:
            def __init__(self, inner: object) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

            async def commit(self) -> None:
                failures.append("commit")
                raise OperationalError("stmt", {}, orig)

        @contextlib.asynccontextmanager
        async def _wrapped() -> AsyncIterator[object]:
            async with real() as sess:
                yield _CommitFails(sess)

        await network.on_connection_status({"is_connected": True, "err": None})
        network.resendable_event.clear()
        task = asyncio.ensure_future(
            network.send_resendable_plaintexts(_conn(fake)),
        )
        await _event("__resend_queue_populated").wait()
        monkeypatch.setattr(persistent, "asession", _wrapped)
        network.resendable_event.set()
        try:
            if fatal:
                await asyncio.wait_for(task, timeout=10.0)
                return
            await _spin_until(partial(bool, failures))
        finally:
            monkeypatch.setattr(persistent, "asession", real)
            network.shutdown()
            network.resendable_event.set()
            if not task.done():
                with contextlib.suppress(OperationalError):
                    await asyncio.wait_for(task, timeout=10.0)


class TestProvisionLegacyRows:
    @pytest.mark.asyncio
    async def test_a_pre_existing_write_cap_cannot_be_converted(
        self, fake_thinclient: _Fake, caplog: pytest.LogCaptureFixture,
    ) -> None:
        kp = await fake_thinclient.new_keypair(b"\x66" * 32)
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.WriteCapWAL(
                id=stream, write_cap=kp.write_cap,
                next_index=kp.first_message_index,
            ))
            sess.add(persistent.ReadCapWAL(id=stream, write_cap_id=stream))
            await sess.commit()
        task = asyncio.ensure_future(
            network.provision_read_caps(_conn(fake_thinclient)),
        )
        try:
            with caplog.at_level(logging.WARNING, logger="katzen.network"):
                await _spin_until(partial(logged, caplog, "DB was created with old API"))
        finally:
            network.shutdown()
            await asyncio.wait_for(task, timeout=10.0)
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            assert rcw.read_cap is None


class TestDuplicateArming:
    @pytest.mark.asyncio
    async def test_two_peers_sharing_a_read_cap_arm_it_once(
        self, fake_thinclient: _Fake, caplog: pytest.LogCaptureFixture,
    ) -> None:
        kp = await fake_thinclient.new_keypair(b"\x77" * 32)
        stream = uuid.uuid4()
        async with persistent.asession() as sess:
            sess.add(persistent.ReadCapWAL(
                id=stream, write_cap_id=None, read_cap=kp.read_cap,
                next_index=kp.first_message_index,
            ))
            sess.add(persistent.ConversationPeer(
                name="peer", read_cap_id=stream,
            ))
            sess.add(persistent.ConversationPeer(
                name="peer-alias", read_cap_id=stream,
            ))
            await sess.commit()
        await network.on_connection_status({"is_connected": True, "err": None})
        _event("__resend_queue_populated").set()
        updated = _event("__mixwal_updated")
        updated.clear()
        task = asyncio.ensure_future(
            network.readables_to_mixwal(_conn(fake_thinclient)),
        )
        try:
            with caplog.at_level(logging.WARNING, logger="katzen.network"):
                await _spin_until(updated.is_set)
        finally:
            network.shutdown()
            network.readables_to_mixwal_event.set()
            await asyncio.wait_for(task, timeout=10.0)
        assert any(
            "skipping duplicate rcw" in r.message for r in caplog.records
        )
        async with persistent.asession() as sess:
            armed = (await sess.exec(
                persistent.select(persistent.MixWAL),
            )).all()
        assert [row.bacap_stream for row in armed] == [stream]


class TestSendSweepCadence:
    @pytest.mark.asyncio
    async def test_the_sweep_runs_without_being_poked(
        self, fake_thinclient: _Fake, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.01)
        await network.on_connection_status({"is_connected": True, "err": None})
        network.resendable_event.clear()
        task = asyncio.ensure_future(
            network.send_resendable_plaintexts(_conn(fake_thinclient)),
        )
        try:
            with caplog.at_level(logging.DEBUG, logger="katzen.network"):
                await _spin_until(partial(logged, caplog, "send_resendable_plaintexts: running"))
        finally:
            network.shutdown()
            network.resendable_event.set()
            await asyncio.wait_for(task, timeout=10.0)
        assert fake_thinclient.call_count("encrypt_write") == 0
