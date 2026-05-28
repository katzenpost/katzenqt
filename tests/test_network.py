import asyncio
import struct
from unittest.mock import MagicMock

import pytest

from katzenqt import network
from katzenqt.network import (
    check_for_new,
    courier_destination_exists,
    create_new_keypair,
    on_connection_status,
    on_error,
    on_message_reply,
    on_message_sent,
    shutdown,
    signal_readables_to_mixwal,
)


# Module-level asyncio events on `network` carry leading double underscores.
# Inside a class body, `network.__foo` would be name-mangled to
# `network._TestX__foo`, so tests reach them through getattr() with a string
# literal, which is not subject to mangling.
_TRACKED_EVENTS = (
    "__should_quit",
    "__mixnet_connected",
    "__mixwal_updated",
    "readables_to_mixwal_event",
    "resendable_event",
)


@pytest.fixture(autouse=True)
def _restore_module_events():
    """Snapshot the module-level events before each test and restore them
    afterwards so a `shutdown()` or `on_connection_status()` call in one
    test does not leak into the next.
    """
    snapshot = {
        name: getattr(network, name).is_set() for name in _TRACKED_EVENTS
    }
    yield
    for name, was_set in snapshot.items():
        ev = getattr(network, name)
        if was_set:
            ev.set()
        else:
            ev.clear()


class TestCourierDestinationExists:
    """Tests for courier_destination_exists() using the new get_all_couriers() API."""

    def _mock_connection(self, couriers):
        conn = MagicMock()
        conn.get_all_couriers.return_value = couriers
        return conn

    def test_matching_destination(self):
        dest = b'\x01' * 32
        conn = self._mock_connection([(dest, b'queue1')])
        assert courier_destination_exists(conn, dest) is True

    def test_non_matching_destination(self):
        dest = b'\x01' * 32
        other = b'\x02' * 32
        conn = self._mock_connection([(other, b'queue1')])
        assert courier_destination_exists(conn, dest) is False

    def test_multiple_couriers(self):
        dest = b'\x03' * 32
        conn = self._mock_connection([
            (b'\x01' * 32, b'q1'),
            (b'\x02' * 32, b'q2'),
            (dest, b'q3'),
        ])
        assert courier_destination_exists(conn, dest) is True

    def test_empty_couriers(self):
        conn = self._mock_connection([])
        assert courier_destination_exists(conn, b'\x01' * 32) is False

    def test_exception_returns_false(self):
        conn = MagicMock()
        conn.get_all_couriers.side_effect = Exception("no PKI")
        assert courier_destination_exists(conn, b'\x01' * 32) is False


class TestCreateNewKeypair:
    """Deterministic derivation of (write_cap, read_cap) from a 32-byte seed."""

    def test_seed_must_be_32_bytes(self):
        with pytest.raises(AssertionError):
            create_new_keypair(b"too short")

    def test_seed_must_be_bytes_not_str(self):
        with pytest.raises(AssertionError):
            create_new_keypair("a" * 32)

    def test_cap_sizes(self):
        write_cap, read_cap = create_new_keypair(b"\x00" * 32)
        assert len(write_cap) == 168
        assert len(read_cap) == 136

    def test_write_cap_embeds_read_cap_in_tail(self):
        write_cap, read_cap = create_new_keypair(b"\x05" * 32)
        assert write_cap[32:] == read_cap

    def test_signing_seed_differs_from_verify_key(self):
        write_cap, read_cap = create_new_keypair(b"\x42" * 32)
        assert write_cap[:32] != read_cap[:32]

    def test_deterministic_for_same_seed(self):
        seed = bytes(range(32))
        first = create_new_keypair(seed)
        second = create_new_keypair(seed)
        assert first == second

    def test_distinct_seeds_yield_distinct_caps(self):
        a_write, a_read = create_new_keypair(b"\x00" * 32)
        b_write, b_read = create_new_keypair(b"\x01" * 32)
        assert a_write != b_write
        assert a_read != b_read

    def test_start_index_top_bit_cleared(self):
        # The first 8 bytes of first_message_index encode start_idx as a
        # little-endian u64 with the high bit forced to zero (see the
        # & 0x7fffffffffffffff mask). SQLite has no unsigned 64-bit type,
        # so this invariant matters for downstream storage.
        _, read_cap = create_new_keypair(b"\xff" * 32)
        first_msg_index = read_cap[32:]
        assert len(first_msg_index) == 104
        (start_idx,) = struct.unpack("<Q", first_msg_index[:8])
        assert start_idx & 0x8000000000000000 == 0


class TestShutdown:
    def test_shutdown_sets_should_quit_event(self):
        ev = getattr(network, "__should_quit")
        ev.clear()
        shutdown()
        assert ev.is_set()


class TestEventSignals:
    @pytest.mark.asyncio
    async def test_signal_readables_to_mixwal_sets_event(self):
        ev = getattr(network, "readables_to_mixwal_event")
        ev.clear()
        await signal_readables_to_mixwal()
        assert ev.is_set()

    @pytest.mark.asyncio
    async def test_check_for_new_sets_resendable_event(self):
        ev = getattr(network, "resendable_event")
        ev.clear()
        await check_for_new()
        assert ev.is_set()


class TestOnConnectionStatus:
    @pytest.mark.asyncio
    async def test_connected_sets_mixnet_connected(self):
        ev = getattr(network, "__mixnet_connected")
        ev.clear()
        await on_connection_status({"is_connected": True, "err": None})
        assert ev.is_set()

    @pytest.mark.asyncio
    async def test_disconnected_clears_mixnet_connected(self):
        ev = getattr(network, "__mixnet_connected")
        ev.set()
        await on_connection_status({"is_connected": False, "err": None})
        assert not ev.is_set()

    @pytest.mark.asyncio
    async def test_err_payload_does_not_raise(self):
        await on_connection_status({
            "is_connected": False,
            "err": {"Op": "read", "Net": "tcp"},
        })

    @pytest.mark.asyncio
    async def test_capital_err_field_does_not_raise(self):
        await on_connection_status({
            "is_connected": False,
            "err": None,
            "Err": {"some": "error"},
        })

    @pytest.mark.asyncio
    async def test_connected_true_with_err_still_sets_event(self):
        # If the daemon reports connected but also surfaces an error
        # field, the event should still reflect connectivity, with the
        # error merely logged.
        ev = getattr(network, "__mixnet_connected")
        ev.clear()
        await on_connection_status({
            "is_connected": True,
            "err": "transient blip",
        })
        assert ev.is_set()


class TestOnMessageReply:
    @pytest.mark.asyncio
    async def test_no_matching_queue_is_silent(self):
        # No listener registered for this message_id: must not raise.
        await on_message_reply(
            {"message_id": b"\x00" * 16, "payload": b"data"}
        )

    @pytest.mark.asyncio
    async def test_matching_queue_receives_reply(self):
        queues = getattr(network, "__on_message_queues")
        message_id = b"\x42" * 16
        q: asyncio.Queue = asyncio.Queue()
        queues[message_id] = q
        try:
            reply = {"message_id": message_id, "payload": b"hello"}
            await on_message_reply(reply)
            received = await asyncio.wait_for(q.get(), timeout=1.0)
            assert received is reply
        finally:
            queues.pop(message_id, None)

    @pytest.mark.asyncio
    async def test_unrelated_queue_not_disturbed(self):
        queues = getattr(network, "__on_message_queues")
        listener_id = b"\xaa" * 16
        intruder_id = b"\xbb" * 16
        q: asyncio.Queue = asyncio.Queue()
        queues[listener_id] = q
        try:
            await on_message_reply(
                {"message_id": intruder_id, "payload": b"not yours"}
            )
            # The listener queue should remain empty.
            assert q.empty()
        finally:
            queues.pop(listener_id, None)


class TestOnMessageSent:
    @pytest.mark.asyncio
    async def test_success_path_does_not_raise(self):
        await on_message_sent({
            "message_id": b"\x00" * 16,
            "surbid": b"\x01" * 16,
            "sent_at": 0,
            "reply_eta": 0,
            "err": None,
        })

    @pytest.mark.asyncio
    async def test_err_path_does_not_raise(self):
        await on_message_sent({
            "message_id": b"\x00" * 16,
            "err": "PKI error: service not found",
        })

    @pytest.mark.asyncio
    async def test_missing_err_key_is_handled(self):
        # Some emissions may omit the err key entirely; reply.get() must
        # cover that without surprising the caller.
        await on_message_sent({"message_id": b"\x00" * 16})


class TestOnError:
    @pytest.mark.asyncio
    async def test_callback_fires_on_exception(self):
        called = []

        async def boom():
            raise RuntimeError("nope")

        task = asyncio.create_task(boom())
        on_error(task, lambda: called.append("fired"))
        with pytest.raises(RuntimeError):
            await task
        assert called == ["fired"]

    @pytest.mark.asyncio
    async def test_callback_skipped_on_success(self):
        called = []

        async def ok():
            return 42

        task = asyncio.create_task(ok())
        on_error(task, lambda: called.append("fired"))
        assert await task == 42
        assert called == []

    @pytest.mark.asyncio
    async def test_returns_the_task(self):
        async def ok():
            return None

        task = asyncio.create_task(ok())
        returned = on_error(task, lambda: None)
        assert returned is task
        await task

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        captured = []

        async def boom():
            raise ValueError("x")

        task = asyncio.create_task(boom())
        on_error(
            task,
            lambda *a, **k: captured.append((a, k)),
            1, 2, key="value",
        )
        with pytest.raises(ValueError):
            await task
        assert captured == [((1, 2), {"key": "value"})]
