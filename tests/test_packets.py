"""Per-packet records behind the Packets window: registry lifecycle, the send
wrapper's status recording, and the Qt table/retention controls."""
import asyncio
import os
import uuid

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenpost_thinclient import (  # noqa: E402
    BoxIDNotFoundError,
    ThinClientOfflineError,
)

from katzenqt import katzen, network, persistent  # noqa: E402
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


def test_attempts_count_retries_per_box():
    network.reset_packets()
    stream = uuid.uuid4()
    first = network.packet_begin(network.PacketContext(
        "contact_read", stream_id=stream, box_index=5))
    retry = network.packet_begin(network.PacketContext(
        "contact_read", stream_id=stream, box_index=5))
    other = network.packet_begin(network.PacketContext(
        "contact_read", stream_id=stream, box_index=6))
    by_id = {r["id"]: r for r in network.packets_snapshot()}
    assert by_id[first]["attempt"] == 1
    assert by_id[retry]["attempt"] == 2  # a retry of the same box
    assert by_id[other]["attempt"] == 1  # a different box


def test_box_position_from_cap():
    first = 1000
    index = first.to_bytes(8, "little") + b"\x00" * (104 - 8)
    read_cap = b"\x00" * 32 + index
    write_cap = b"\x00" * 64 + index
    assert network._box_position(1002, read_cap) == 3
    assert network._box_position(1000, write_cap) == 1
    assert network._box_position(None, read_cap) is None
    assert network._box_position(5, b"short") is None


@pytest.mark.asyncio
async def test_wrapper_records_a_read_payload():
    network.reset_packets()
    network.reset_stats()
    conn = _StubConnection(read_plaintext=b"Cdata")
    network.install_stats_counters(conn)
    context = network.PacketContext(
        "contact_read", stream_id=uuid.uuid4(), box_index=7,
        box_position=3, timeout_s=1200,
    )
    await conn.start_resending_encrypted_message(
        read_cap=b"r", write_cap=None, _packet_context=context,
    )
    record = _only_record()
    assert record["status"] == network.PACKET_STATUS_PAYLOAD
    assert record["kind"] == "contact_read"
    assert record["box_index"] == 7
    assert record["box_position"] == 3
    assert record["attempt"] == 1


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


def test_packets_dialog_keeps_the_selected_packet_across_refresh():
    """In-flight churn resets the model; the dialog reselects by packet id so
    the user's selection survives the tick."""
    app = QApplication.instance() or QApplication([])
    network.reset_packets()
    network.packet_begin(network.PacketContext("write"))
    dialog = katzen.PacketsDialog(None)
    dialog._table.selectRow(0)
    before = [
        dialog._model._rows[i.row()]["id"]
        for i in dialog._table.selectionModel().selectedRows()
    ]
    assert before

    # A new in-flight packet changes the id set, forcing a reset.
    network.packet_begin(network.PacketContext("write"))
    dialog._refresh()
    after = [
        dialog._model._rows[i.row()]["id"]
        for i in dialog._table.selectionModel().selectedRows()
    ]
    assert after == before
    dialog.deleteLater()
    _ = app


def _seed_conversation_streams():
    """One conversation with a main write stream, a contact peer, and an agg
    substream (25 chunks). Returns (main, contact_rcw_id, agg)."""
    main = uuid.uuid4()
    own_rcw = uuid.uuid4()
    contact_rcw = uuid.uuid4()
    agg = uuid.uuid4()
    indirection = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.WriteCapWAL(
            id=main, write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
        ))
        sess.add(persistent.ReadCapWAL(
            id=own_rcw, write_cap_id=main,
            read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
        ))
        conv = persistent.Conversation(name="c", write_cap=main, first_unread=0)
        own_peer = persistent.ConversationPeer(
            name="bob", read_cap_id=own_rcw, active=False, conversation=conv,
        )
        conv.own_peer = own_peer
        sess.add(conv)
        sess.add(own_peer)
        sess.flush()  # insert own_peer then conv (own_peer_id FK)
        sess.add(persistent.ReadCapWAL(
            id=contact_rcw, read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
        ))
        sess.add(persistent.ConversationPeer(
            name="alice", read_cap_id=contact_rcw, active=True, conversation=conv,
        ))
        sess.add(persistent.WriteCapWAL(
            id=agg, write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
        ))
        sess.add(persistent.ReadCapWAL(
            id=indirection, write_cap_id=agg, read_cap=b"\x00" * 136,
            next_index=b"\x00" * 104, substream_total_chunks=25,
        ))
        sess.add(persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=agg, conversation_id=conv.id,
            bacap_payload=b"Cchunk",
        ))
        i_chunk_id = uuid.uuid4()
        sess.add(persistent.PlaintextWAL(
            id=i_chunk_id, bacap_stream=main, conversation_id=conv.id,
            bacap_payload=b"", indirection=indirection,
        ))
        sess.add(persistent.ConversationLog(
            id=uuid.uuid4(), conversation_id=conv.id,
            conversation_peer_id=own_peer.id, conversation_order=0,
            payload=b"F" + cbor2.dumps({
                "v": 0, "kind": "file_outgoing", "basename": "photo.jpg",
            }),
            network_status=1, outgoing_pwal=i_chunk_id,
        ))
        sess.commit()
    return main, contact_rcw, agg


def test_file_marker_basename():
    marker = b"F" + cbor2.dumps({
        "kind": "file_outgoing", "basename": "x.jpg",
    })
    assert network._file_marker_basename(marker) == "x.jpg"
    assert network._file_marker_basename(
        b"F" + cbor2.dumps({"kind": "file_marker", "basename": "x.jpg"})
    ) is None
    assert network._file_marker_basename(b"Fnotcbor") is None
    assert network._file_marker_basename(b"hello") is None


def test_stream_info_labels_and_substream_total():
    app = QApplication.instance() or QApplication([])
    main, contact_rcw, agg = _seed_conversation_streams()
    model = PacketsModel()
    # A write to our own message stream: "<own name> in <conversation>".
    assert model._query_stream_info(main) == ("bob in c", None)
    # A contact read: the contact's name.
    assert model._query_stream_info(contact_rcw) == ("alice", None)
    # An agg substream write: the file's name, and its total chunk count.
    assert model._query_stream_info(agg) == ("photo.jpg (in c)", 25)
    _ = app


def test_upload_label_captured_at_send_time_wins():
    app = QApplication.instance() or QApplication([])
    network.reset_packets()
    stream = uuid.uuid4()
    network.set_upload_label(stream, "photo.jpg (in c)")
    assert network.upload_label(stream) == "photo.jpg (in c)"
    network.packet_begin(network.PacketContext(
        "write", stream_id=stream, box_index=1, box_position=1,
        label=network.upload_label(stream),
    ))
    model = PacketsModel()
    model.refresh()
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == (
        "photo.jpg (in c)"
    )
    _ = app


def test_position_over_total_for_substream_packets():
    app = QApplication.instance() or QApplication([])
    main, _contact_rcw, agg = _seed_conversation_streams()
    network.reset_packets()
    network.set_packet_finished_limit(5)
    network.packet_begin(network.PacketContext(
        "write", stream_id=agg, box_index=5, box_position=3,
    ))
    network.packet_begin(network.PacketContext(
        "write", stream_id=main, box_index=5, box_position=2,
    ))
    model = PacketsModel()
    model.refresh()
    # The substream row shows position/total; the main-stream row position only.
    pos = {model.data(model.index(r, 3), Qt.ItemDataRole.DisplayRole):
           model.data(model.index(r, 4), Qt.ItemDataRole.DisplayRole)
           for r in range(model.rowCount())}
    assert pos["photo.jpg (in c)"] == "3/25"
    assert pos["bob in c"] == "2"
    _ = app


def test_retry_column_counts_retries():
    app = QApplication.instance() or QApplication([])
    network.reset_packets()
    network.set_packet_finished_limit(5)
    stream = uuid.uuid4()
    network.packet_begin(network.PacketContext(
        "contact_read", stream_id=stream, box_index=5))
    network.packet_begin(network.PacketContext(
        "contact_read", stream_id=stream, box_index=5))
    model = PacketsModel()
    model.refresh()
    retries = {
        model.data(model.index(r, 6), Qt.ItemDataRole.DisplayRole)
        for r in range(model.rowCount())
    }
    assert retries == {"0", "1"}
    _ = app
