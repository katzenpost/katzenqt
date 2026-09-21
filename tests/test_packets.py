"""Per-packet records behind the Packets window: registry lifecycle, the send
wrapper's status recording, and the Qt table/retention controls."""
import asyncio
import os
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenpost_thinclient import (  # noqa: E402
    BoxIDNotFoundError,
    ThinClientOfflineError,
)

from katzenqt import katzen, network  # noqa: E402
from katzenqt.qt_models import PacketsModel  # noqa: E402


class _StubConnection:
    def __init__(self, *, read_plaintext: bytes = b"", boxnotfound: bool = False,
                 link_down: bool = False, gate=None):
        self.read_plaintext = read_plaintext
        self.boxnotfound = boxnotfound
        self.link_down = link_down
        self.gate = gate

    async def encrypt_read(self, **kwargs):
        return object()

    async def encrypt_write(self, **kwargs):
        return object()

    async def start_resending_encrypted_message(self, **kwargs):
        if self.gate is not None:
            await self.gate.wait()
        if self.link_down:
            raise ThinClientOfflineError()
        if self.boxnotfound:
            raise BoxIDNotFoundError()
        if kwargs.get("write_cap") is not None:
            return type("R", (), {"plaintext": b""})()
        return type("R", (), {"plaintext": self.read_plaintext})()


def _only_record() -> dict:
    snapshot = network.packets_snapshot()
    assert len(snapshot) == 1
    return snapshot[0]


async def _wait_in_flight() -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if any(
            r["status"] == network.PACKET_STATUS_IN_FLIGHT
            for r in network.packets_snapshot()
        ):
            return
    raise AssertionError("packet never became in-flight")


def test_registry_prunes_finished_to_the_limit():
    network.reset_packets()
    network.set_packet_finished_limit(2)
    ids = [network.packet_begin(network.PacketContext("write")) for _ in range(3)]
    for packet_id in ids:
        network.packet_finish(packet_id, network.PACKET_STATUS_ACKED)
    live = {r["id"] for r in network.packets_snapshot()}
    assert ids[0] not in live  # oldest finished pruned
    assert ids[1] in live and ids[2] in live


def test_registry_keeps_in_flight_even_at_zero_limit():
    network.reset_packets()
    network.set_packet_finished_limit(0)
    inflight = network.packet_begin(network.PacketContext("write"))
    finished = network.packet_begin(network.PacketContext("write"))
    network.packet_finish(finished, network.PACKET_STATUS_ACKED)
    live = {r["id"] for r in network.packets_snapshot()}
    assert inflight in live
    assert finished not in live


def test_clear_finished_keeps_in_flight():
    network.reset_packets()
    network.set_packet_finished_limit(5)
    inflight = network.packet_begin(network.PacketContext("write"))
    finished = network.packet_begin(network.PacketContext("write"))
    network.packet_finish(finished, network.PACKET_STATUS_ACKED)
    network.clear_finished_packets()
    assert {r["id"] for r in network.packets_snapshot()} == {inflight}


def test_ordinals_count_per_stream():
    network.reset_packets()
    stream = uuid.uuid4()
    first = network.packet_begin(
        network.PacketContext("contact_read", stream_id=stream))
    second = network.packet_begin(
        network.PacketContext("contact_read", stream_id=stream))
    by_id = {r["id"]: r for r in network.packets_snapshot()}
    assert by_id[first]["ordinal"] == 1
    assert by_id[second]["ordinal"] == 2


@pytest.mark.asyncio
async def test_wrapper_records_a_read_payload():
    network.reset_packets()
    network.reset_stats()
    conn = _StubConnection(read_plaintext=b"Cdata")
    network.install_stats_counters(conn)
    context = network.PacketContext(
        "contact_read", stream_id=uuid.uuid4(), box_index=7, timeout_s=1200,
    )
    await conn.start_resending_encrypted_message(
        read_cap=b"r", write_cap=None, _packet_context=context,
    )
    record = _only_record()
    assert record["status"] == network.PACKET_STATUS_PAYLOAD
    assert record["kind"] == "contact_read"
    assert record["box_index"] == 7
    assert record["ordinal"] == 1
    assert record["detail"] == "5 B (C)"


@pytest.mark.asyncio
async def test_wrapper_records_boxnotfound_and_link_down():
    network.reset_packets()
    network.reset_stats()
    conn = _StubConnection(boxnotfound=True)
    network.install_stats_counters(conn)
    with pytest.raises(BoxIDNotFoundError):
        await conn.start_resending_encrypted_message(
            read_cap=b"r", write_cap=None,
            _packet_context=network.PacketContext("contact_read"),
        )
    assert _only_record()["status"] == network.PACKET_STATUS_BOXNOTFOUND

    network.reset_packets()
    conn = _StubConnection(link_down=True)
    network.install_stats_counters(conn)
    with pytest.raises(ThinClientOfflineError):
        await conn.start_resending_encrypted_message(
            read_cap=b"r", write_cap=None,
            _packet_context=network.PacketContext("contact_read"),
        )
    assert _only_record()["status"] == network.PACKET_STATUS_LINK_DOWN


@pytest.mark.asyncio
async def test_wrapper_records_a_write_ack():
    network.reset_packets()
    network.reset_stats()
    conn = _StubConnection()
    network.install_stats_counters(conn)
    await conn.start_resending_encrypted_message(
        read_cap=None, write_cap=b"w",
        _packet_context=network.PacketContext("write"),
    )
    assert _only_record()["status"] == network.PACKET_STATUS_ACKED


@pytest.mark.asyncio
async def test_wrapper_distinguishes_timeout_from_cancel():
    network.reset_packets()
    conn = _StubConnection(gate=asyncio.Event())
    network.install_stats_counters(conn)
    context = network.PacketContext("write")
    task = asyncio.create_task(conn.start_resending_encrypted_message(
        read_cap=None, write_cap=b"w", _packet_context=context,
    ))
    await _wait_in_flight()
    context.timed_out = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _only_record()["status"] == network.PACKET_STATUS_TIMED_OUT

    network.reset_packets()
    gate = asyncio.Event()
    conn = _StubConnection(gate=gate)
    network.install_stats_counters(conn)
    context = network.PacketContext("write")
    task = asyncio.create_task(conn.start_resending_encrypted_message(
        read_cap=None, write_cap=b"w", _packet_context=context,
    ))
    await _wait_in_flight()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _only_record()["status"] == network.PACKET_STATUS_CANCELLED


def test_packets_model_lists_in_flight_first_and_clear_works():
    app = QApplication.instance() or QApplication([])
    network.reset_packets()
    network.set_packet_finished_limit(5)
    inflight = network.packet_begin(network.PacketContext("write"))
    finished = network.packet_begin(network.PacketContext("write"))
    network.packet_finish(finished, network.PACKET_STATUS_ACKED)

    model = PacketsModel()
    model.refresh()
    assert model.rowCount() == 2
    assert model.columnCount() == len(katzen.PACKET_COLUMNS)
    assert model.data(model.index(0, 5), Qt.ItemDataRole.DisplayRole) == "In flight"
    assert model.data(model.index(1, 5), Qt.ItemDataRole.DisplayRole) == "ACKed"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "write"

    network.clear_finished_packets()
    model.refresh()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 5), Qt.ItemDataRole.DisplayRole) == "In flight"
    assert inflight == model._rows[0]["id"]
    _ = app


def test_packets_dialog_retention_combo_and_clear():
    app = QApplication.instance() or QApplication([])
    network.reset_packets()
    dialog = katzen.PacketsDialog(None)
    assert dialog._limit_combo.currentData() == network.DEFAULT_PACKET_FINISHED_LIMIT

    index = dialog._limit_combo.findData(0)
    dialog._limit_combo.setCurrentIndex(index)
    assert network.get_packet_finished_limit() == 0

    finished = network.packet_begin(network.PacketContext("write"))
    network.packet_finish(finished, network.PACKET_STATUS_ACKED)
    dialog._clear_finished()
    assert network.packets_snapshot() == []
    dialog.deleteLater()
    _ = app
