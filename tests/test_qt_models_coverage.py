from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QModelIndex,
    QObject,
    Qt,
)
from PySide6.QtCore import QAbstractItemModel, QAbstractTableModel  # noqa: E402
from PySide6.QtCore import QSortFilterProxyModel  # noqa: E402
from PySide6.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLineEdit,
    QMainWindow,
    QTreeView,
)

from katzenqt import models, network, persistent, qt_models  # noqa: E402
from katzenqt.qt_models import (  # noqa: E402
    ROLE_CHAT_ATTACHMENT_BASENAME,
    ROLE_CHAT_AUTHOR,
    ROLE_CHAT_IS_TALLY,
    ROLE_CHAT_MESSAGE_ID,
    ROLE_CHAT_NETWORK_STATUS,
    ROLE_TRANSFER_PARENT_NAME,
    ROLE_TRANSFER_RATE,
    ConversationLogModel,
    ConversationUIState,
    DownloadsModel,
    FilterProxyModel,
    PacketsModel,
    format_rate,
)
from katzenqt.tally import events, schema, sync  # noqa: E402
from katzenqt.tally.controller import voter_id_from_read_cap  # noqa: E402
from functools import partial
from tests.stubs import appending

OWN_CAP = bytes([0x01]) * 136
DISPLAY = Qt.ItemDataRole.DisplayRole
EM_DASH = "\u2014"


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


def _make_conversation(name: str = "lobby") -> tuple[int, int]:
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcap.id, read_cap=OWN_CAP,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=own_rcap.id, conversation=convo,
    )
    convo.own_peer = own_peer
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        sess.commit()
        return convo.id, own_peer.id


def _seed_log(
    convo_id: int,
    peer_id: int,
    order: int,
    payload: bytes,
    network_status: int = 0,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            id=row_id, conversation_id=convo_id, conversation_peer_id=peer_id,
            conversation_order=order, payload=payload,
            network_status=network_status,
        ))
        sess.commit()
    return row_id


def _text_payload(text: str) -> bytes:
    body: bytes = models.GroupChatMessage(
        version=0, membership_hash=bytes(32), text=text,
    ).to_cbor()
    return b"F" + body


class _FilterUi:
    def __init__(self) -> None:
        self.contacts_treeWidget = QTreeView()
        self.contactFilterLineEdit = QLineEdit()


class _FilterWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.ui = _FilterUi()


def _contacts_tree() -> QStandardItemModel:
    source = QStandardItemModel()
    alice = QStandardItem("alice")
    alice.appendRow(QStandardItem("bob"))
    source.appendRow(alice)
    source.appendRow(QStandardItem("carol"))
    return source


def test_filter_accepts_a_directly_matching_top_level_row() -> None:
    window = _FilterWindow()
    proxy = FilterProxyModel(window)
    proxy.setSourceModel(_contacts_tree())

    window.ui.contactFilterLineEdit.setText("ALI")
    assert proxy.filterAcceptsRow(0, QModelIndex()) is True
    assert proxy.filterAcceptsRow(1, QModelIndex()) is False


def test_filter_keeps_children_of_a_matching_parent() -> None:
    window = _FilterWindow()
    proxy = FilterProxyModel(window)
    source = _contacts_tree()
    proxy.setSourceModel(source)

    window.ui.contactFilterLineEdit.setText("ali")
    alice = source.index(0, 0, QModelIndex())
    assert proxy.filterAcceptsRow(0, alice) is True

    window.ui.contactFilterLineEdit.setText("zz")
    assert proxy.filterAcceptsRow(0, alice) is False


def test_filter_keeps_a_parent_whose_child_matches() -> None:
    window = _FilterWindow()
    proxy = FilterProxyModel(window)
    proxy.setSourceModel(_contacts_tree())

    window.ui.contactFilterLineEdit.setText("bo")
    assert proxy.filterAcceptsRow(0, QModelIndex()) is True
    assert proxy.filterAcceptsRow(1, QModelIndex()) is False


def test_invalidate_re_expands_the_contacts_tree() -> None:
    window = _FilterWindow()
    proxy: QSortFilterProxyModel = FilterProxyModel(window)
    proxy.setSourceModel(_contacts_tree())
    window.ui.contacts_treeWidget.setModel(proxy)
    window.ui.contacts_treeWidget.collapseAll()

    proxy.invalidate()
    assert window.ui.contacts_treeWidget.isExpanded(
        proxy.index(0, 0, QModelIndex()),
    ) is True


def test_format_rate_falls_through_to_gibibytes() -> None:
    assert format_rate(2 * 1024 ** 3) == "2.0 GiB/s"
    assert format_rate(-5.0) == "0 B/s"


def test_downloads_rowcount_is_zero_under_a_valid_parent() -> None:
    model = DownloadsModel()
    model.start_transfer(uuid.uuid4(), conversation_id=1, parent_name="a", total=1)
    assert model.rowCount(model.index(0, 0)) == 0


def test_downloads_header_data_ignores_other_roles_and_orientations() -> None:
    model = DownloadsModel()
    assert model.headerData(
        0, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole,
    ) is None
    assert model.headerData(0, Qt.Orientation.Vertical, DISPLAY) is None


def test_downloads_data_returns_none_for_extra_columns_and_roles() -> None:
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=1, parent_name="a", total=1)
    assert model.data(model.createIndex(0, 4), DISPLAY) is None
    assert model.data(model.index(0, 0), 0xBEEF) is None


def test_downloads_rate_role_matches_the_rate_column() -> None:
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=1, parent_name="a", total=1)
    model.fail_transfer(rcw_id, "boom")
    assert model.data(model.index(0, 0), ROLE_TRANSFER_RATE) == "0 B/s"


def test_complete_and_pause_ignore_unknown_rows() -> None:
    model = DownloadsModel()
    model.complete_transfer(uuid.uuid4())
    model.set_paused(uuid.uuid4(), paused=True)
    assert model.rowCount() == 0


def _add_substream_peer(
    convo_id: int, peer_name: str, read_cap_id: uuid.UUID,
) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        convo = sess.get(persistent.Conversation, convo_id)
        assert convo is not None
        sess.add(persistent.ConversationPeer(
            name=peer_name, read_cap_id=read_cap_id, active=True,
            conversation=convo,
        ))
        sess.commit()


def test_seed_skips_a_substream_whose_read_cap_row_is_gone() -> None:
    convo_id, _ = _make_conversation("lobby")
    _add_substream_peer(
        convo_id, f"{network._SUBSTREAM_NAME_PREFIX}1:aa", uuid.uuid4(),
    )
    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 0


def test_seed_falls_back_to_the_conversation_name_without_a_parent_peer() -> None:
    convo_id, _ = _make_conversation("lobby")
    rcw_id = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ReadCapWAL(
            id=rcw_id, read_cap=b"\x11" * 136, substream_total_chunks=2,
        ))
        sess.commit()
    _add_substream_peer(
        convo_id, f"{network._SUBSTREAM_NAME_PREFIX}999999:aa", rcw_id,
    )

    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), ROLE_TRANSFER_PARENT_NAME) == "lobby"


def test_seed_skips_an_indirection_without_a_write_cap() -> None:
    convo_id, _ = _make_conversation("lobby")
    rcw_id = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ReadCapWAL(
            id=rcw_id, write_cap_id=None, read_cap=b"\x22" * 136,
        ))
        sess.add(persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=uuid.uuid4(),
            conversation_id=convo_id, bacap_payload=b"I",
            indirection=rcw_id,
        ))
        sess.commit()

    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 0


class _FrozenTime:

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def localtime(self, seconds: float) -> time.struct_time:
        return time.localtime(seconds)

    def strftime(self, fmt: str, when: time.struct_time) -> str:
        return time.strftime(fmt, when)


def _packet(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "p1",
        "kind": "mixwal_read",
        "stream_id": None,
        "box_index": None,
        "box_position": None,
        "attempt": 1,
        "sent_at": 900.0,
        "sent_wall": 1_700_000_000.0,
        "timeout_s": None,
        "status": "acked",
        "finished_at": 905.0,
        "envelope_hash": b"",
        "stage": None,
        "label": None,
    }
    row.update(overrides)
    return row


def _packets_model(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]],
) -> PacketsModel:
    monkeypatch.setattr(network, "packets_snapshot", partial(list, rows))
    model = PacketsModel()
    model.refresh()
    return model


def test_packets_rowcount_and_data_reject_invalid_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _packets_model(monkeypatch, [_packet()])
    base: QAbstractTableModel = model
    assert base.rowCount(base.index(0, 0)) == 0
    assert base.data(QModelIndex(), DISPLAY) is None
    assert base.data(base.index(0, 0), Qt.ItemDataRole.ToolTipRole) is None
    assert base.headerData(0, Qt.Orientation.Horizontal, DISPLAY) == "Sent"
    assert base.headerData(0, Qt.Orientation.Vertical, DISPLAY) is None


def test_packets_cells_render_time_direction_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qt_models, "time", _FrozenTime(1000.0))
    rows = [
        _packet(id="r", kind="mixwal_read", sent_wall=1_700_000_000.0),
        _packet(id="w", kind="mixwal_write", sent_wall=1_700_000_000.0),
    ]
    model = _packets_model(monkeypatch, rows)
    base: QAbstractTableModel = model
    expected_clock = time.strftime(
        "%H:%M:%S", time.localtime(1_700_000_000.0),
    )
    assert base.data(base.index(0, 0), DISPLAY) == expected_clock
    kinds = {
        str(base.data(base.index(r, 2), DISPLAY)): base.data(
            base.index(r, 1), DISPLAY,
        )
        for r in range(2)
    }
    assert kinds == {"mixwal read": "Read", "mixwal write": "Write"}


def test_packets_position_column_falls_back_and_blanks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _packet(id="a", box_position=None, box_index=7),
        _packet(id="b", box_position=None, box_index=None),
    ]
    model = _packets_model(monkeypatch, rows)
    base: QAbstractTableModel = model
    assert base.data(base.index(0, 4), DISPLAY) == "7"
    assert base.data(base.index(1, 4), DISPLAY) == EM_DASH


def test_packets_duration_and_timeout_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qt_models, "time", _FrozenTime(1000.0))
    rows = [
        _packet(
            id="live", status="in_flight", sent_at=900.0, finished_at=None,
            timeout_s=130.0, attempt=3,
        ),
        _packet(
            id="late", status="in_flight", sent_at=900.0, finished_at=None,
            timeout_s=10.0,
        ),
        _packet(id="done", sent_at=900.0, finished_at=905.0, timeout_s=10.0),
    ]
    model = _packets_model(monkeypatch, rows)
    base: QAbstractTableModel = model
    order = [str(base.data(base.index(r, 5), DISPLAY)) for r in range(3)]
    assert order == ["In flight", "In flight", "ACKed"]

    assert base.data(base.index(0, 6), DISPLAY) == "2"
    assert base.data(base.index(0, 7), DISPLAY) == "1m 40s"
    assert base.data(base.index(0, 8), DISPLAY) == "30s"
    assert base.data(base.index(1, 8), DISPLAY) == "overdue"
    assert base.data(base.index(2, 7), DISPLAY) == "5s"
    assert base.data(base.index(2, 8), DISPLAY) == EM_DASH
    assert base.data(model.createIndex(2, 9), DISPLAY) is None


def test_packets_refresh_repaints_in_place_when_the_ids_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _packets_model(monkeypatch, [_packet()])
    changed: list[bool] = []
    resets: list[bool] = []
    model.dataChanged.connect(appending(changed, True))
    model.modelReset.connect(appending(resets, True))

    model.refresh()
    assert changed == [True]
    assert resets == []


def test_packets_stream_label_for_vouchers_and_streamless_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _packet(id="v", kind="voucher_write", stage="handshake"),
        _packet(id="v2", kind="voucher_read", stage=None),
        _packet(id="n", kind="mixwal_read", stream_id=None),
    ]
    model = _packets_model(monkeypatch, rows)
    base: QAbstractTableModel = model
    labels = [str(base.data(base.index(r, 3), DISPLAY)) for r in range(3)]
    assert labels == ["handshake", "voucher", EM_DASH]


def _seed_substream_stream(convo_id: int, parent_name: str | None) -> uuid.UUID:
    stream_id = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        convo = sess.get(persistent.Conversation, convo_id)
        assert convo is not None
        sess.add(persistent.ReadCapWAL(
            id=stream_id, read_cap=b"\x33" * 136, substream_total_chunks=4,
        ))
        parent_id = 999999
        if parent_name is not None:
            parent = persistent.ConversationPeer(
                name=parent_name, read_cap_id=uuid.uuid4(), conversation=convo,
            )
            sess.add(parent)
            sess.flush()
            assert parent.id is not None
            parent_id = parent.id
        sess.add(persistent.ConversationPeer(
            name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:aa",
            read_cap_id=stream_id, conversation=convo,
        ))
        sess.commit()
    return stream_id


def test_packets_label_names_the_substreams_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    convo_id, _ = _make_conversation("lobby")
    stream_id = _seed_substream_stream(convo_id, "carol")
    model = _packets_model(
        monkeypatch, [_packet(stream_id=stream_id, box_position=1)],
    )
    base: QAbstractTableModel = model
    assert base.data(base.index(0, 3), DISPLAY) == "substream of carol"
    assert base.data(base.index(0, 4), DISPLAY) == "1/4"


def test_packets_label_for_a_substream_with_no_parent_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    convo_id, _ = _make_conversation("lobby")
    stream_id = _seed_substream_stream(convo_id, None)
    model = _packets_model(monkeypatch, [_packet(stream_id=stream_id)])
    base: QAbstractTableModel = model
    assert base.data(base.index(0, 3), DISPLAY) == "substream"


def test_packets_label_for_an_agg_write_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    convo_id, _ = _make_conversation("lobby")
    named_agg = uuid.uuid4()
    bare_agg = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add_all((
            persistent.WriteCapWAL(id=named_agg, write_cap=b"\x01" * 168),
            persistent.WriteCapWAL(id=bare_agg, write_cap=b"\x02" * 168),
            persistent.ReadCapWAL(
                id=uuid.uuid4(), write_cap_id=named_agg,
                read_cap=b"\x44" * 136, substream_total_chunks=6,
            ),
            persistent.ReadCapWAL(
                id=uuid.uuid4(), write_cap_id=bare_agg,
                read_cap=b"\x55" * 136, substream_total_chunks=7,
            ),
            persistent.PlaintextWAL(
                id=uuid.uuid4(), bacap_stream=named_agg,
                conversation_id=convo_id, bacap_payload=b"Cchunk",
            ),
        ))
        sess.commit()

    named = _packets_model(
        monkeypatch, [_packet(stream_id=named_agg, box_position=2)],
    )
    named_base: QAbstractTableModel = named
    assert named_base.data(named_base.index(0, 3), DISPLAY) == (
        "substream of lobby"
    )
    assert named_base.data(named_base.index(0, 4), DISPLAY) == "2/6"

    bare = _packets_model(monkeypatch, [_packet(stream_id=bare_agg)])
    bare_base: QAbstractTableModel = bare
    assert bare_base.data(bare_base.index(0, 3), DISPLAY) == "substream"


def test_index_rejects_parented_and_out_of_range_cells() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("hi"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()

    valid = model.index(0, 0, QModelIndex())
    assert valid.isValid() is True
    assert model.index(0, 0, valid).isValid() is False
    assert model.index(1, 0, QModelIndex()).isValid() is False
    assert model.index(-1, 0, QModelIndex()).isValid() is False
    assert model.index(0, -1, QModelIndex()).isValid() is False
    assert model.parent(valid).isValid() is False
    assert model.rowCount(valid) == 0
    assert model.columnCount(QModelIndex()) == 1
    assert model.columnCount(valid) == 0


def test_order_for_row_on_an_empty_model_is_zero() -> None:
    model = ConversationLogModel(4242)
    assert model.order_for_row(0) == 0
    assert model.order_for_row(9) == 0


def test_empty_model_refresh_and_tally_repaint_emit_nothing() -> None:
    convo_id, _ = _make_conversation()
    model = ConversationLogModel(convo_id)
    changed: list[bool] = []
    model.dataChanged.connect(appending(changed, True))

    assert model.refresh_row_count() is False
    model.refresh_tally_rows()
    assert changed == []


def test_header_data_and_flags() -> None:
    model = ConversationLogModel(1)
    assert model.headerData(
        0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole,
    ) == "header1"
    assert model.headerData(
        0, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole,
    ) is None
    assert model.flags(model.createIndex(0, 0)) == Qt.ItemFlag.NoItemFlags


def test_author_and_network_status_roles() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("sent"), network_status=1)
    _seed_log(convo_id, peer_id, 1, _text_payload("acked"), network_status=2)
    model = ConversationLogModel(convo_id)
    model.set_row_count()

    pending = model.index(0, 0, QModelIndex())
    acked = model.index(1, 0, QModelIndex())
    assert model.data(pending, ROLE_CHAT_AUTHOR) == "me"
    assert model.data(acked, ROLE_CHAT_AUTHOR) == "me"
    assert model.data(pending, ROLE_CHAT_NETWORK_STATUS) == 1
    assert model.data(acked, ROLE_CHAT_NETWORK_STATUS) == 2


def test_a_settled_network_status_is_answered_from_the_stable_cache() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("acked"), network_status=2)
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())
    assert model.data(index, ROLE_CHAT_NETWORK_STATUS) == 2

    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.exec(
            persistent.select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id,
            )
        ).one()
        row.network_status = 0
        sess.add(row)
        sess.commit()

    assert model.data(index, ROLE_CHAT_NETWORK_STATUS) == 2
    model.set_row_count()
    assert model.data(index, ROLE_CHAT_NETWORK_STATUS) == 0


def test_data_returns_none_when_the_row_vanished_under_the_count() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("gone"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())

    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.exec(
            persistent.select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id,
            )
        ).one()
        sess.delete(row)
        sess.commit()

    assert model.rowCount(QModelIndex()) == 1
    assert model.data(index, ROLE_CHAT_MESSAGE_ID) is None


def test_a_pre_protocol_row_renders_as_plain_text() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, b"hello from before the wrapper")
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())
    assert model.data(index, 0) == "hello from before the wrapper"
    assert model.data(index, ROLE_CHAT_IS_TALLY) is None


def test_a_plain_text_row_is_not_treated_as_a_tally_row() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("just chatting"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())
    assert model.data(index, 0) == "just chatting"
    assert model.data(index, ROLE_CHAT_IS_TALLY) is None


def test_an_introduction_row_announces_the_added_peer() -> None:
    convo_id, peer_id = _make_conversation()
    intro = models.GroupChatPleaseAdd(display_name="bob", read_cap=bytes(136))
    payload = b"F" + models.GroupChatMessage(
        version=0, membership_hash=bytes(32), introduction=intro,
    ).to_cbor()
    _seed_log(convo_id, peer_id, 0, payload)
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    assert model.data(model.index(0, 0, QModelIndex()), 0) == "me added bob"


def test_tally_rows_have_no_attachment_roles() -> None:
    convo_id, peer_id = _make_conversation()
    survey_id = uuid.uuid4().bytes
    doc = schema.new_survey_doc(
        survey_id, "lunch?", schema.Mode.APPROVAL, ("a", "b"),
        creator=voter_id_from_read_cap(OWN_CAP),
    )
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=survey_id, conversation_id=convo_id,
            doc_state=sync.full_state(doc),
        ))
        sess.commit()
    _seed_log(
        convo_id, peer_id, 0,
        b"F" + events.build_create(survey_id, sync.full_state(doc)).to_cbor(),
    )

    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())
    assert model.data(index, ROLE_CHAT_IS_TALLY) is True
    assert model.data(index, ROLE_CHAT_ATTACHMENT_BASENAME) is None


def test_an_undecodable_survey_blob_renders_the_row_as_unknown() -> None:
    convo_id, peer_id = _make_conversation()
    survey_id = uuid.uuid4().bytes
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=survey_id, conversation_id=convo_id,
            doc_state=b"not a crdt update",
        ))
        sess.commit()
    _seed_log(
        convo_id, peer_id, 0,
        b"F" + events.build_vote(survey_id, {"s0": "yes"}).to_cbor(),
    )

    model = ConversationLogModel(convo_id)
    model.set_row_count()
    assert model.data(model.index(0, 0, QModelIndex()), 0) == (
        f"me: vote for unknown poll {survey_id.hex()}"
    )


def test_an_oversized_attachment_marker_reports_its_size() -> None:
    convo_id, peer_id = _make_conversation()
    payload = b"F" + cbor2.dumps({
        "v": 0, "kind": "file_oversized", "basename": "huge.bin",
        "filetype": "application/octet-stream", "size": 3 * 1024 * 1024,
    })
    _seed_log(convo_id, peer_id, 0, payload)
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    index = model.index(0, 0, QModelIndex())
    assert model.data(index, 0) == "[attachment too large] huge.bin (3.0 MiB)"
    assert model.data(index, qt_models.ROLE_CHAT_ATTACHMENT_KIND) == "oversized"


def _ui_state(convo_id: int, model: ConversationLogModel) -> ConversationUIState:
    return ConversationUIState(
        conversation_id=convo_id,
        own_peer_id=1,
        own_peer_name="me",
        own_peer_bacap_uuid=uuid.uuid4(),
        chat_lineEdit_buffer="",
        conversation_log_model=model,
        contacts_standard_item=QStandardItem("lobby"),
    )


def test_qml_ctx_exposes_settings_and_the_unread_row() -> None:
    convo_id, peer_id = _make_conversation()
    for order in (0, 2, 5):
        _seed_log(convo_id, peer_id, order, _text_payload("x"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    state = _ui_state(convo_id, model)
    state.first_unread = 5

    root = QObject()
    props = state.qml_ctx(root, {"contactName.font.pointSize": 17})

    assert props.value("chatTreeViewModel") is model
    assert props.value("conversation_scroll") == 0.0
    assert props.value("first_unread") == 2
    assert props.value("chat_text_size") == 11
    assert props.value("contact_name_text_size") == 17


def test_qml_ctx_defaults_the_contact_name_size() -> None:
    convo_id, _ = _make_conversation()
    state = _ui_state(convo_id, ConversationLogModel(convo_id))
    props = state.qml_ctx(None, {})
    assert props.value("contact_name_text_size") == 11


def test_mark_first_unread_reports_only_real_changes() -> None:
    convo_id, _ = _make_conversation()
    state = _ui_state(convo_id, ConversationLogModel(convo_id))
    assert state.mark_first_unread(0) is False
    assert state.mark_first_unread(3) is True
    assert state.first_unread == 3


def test_adopt_first_unread_row_maps_a_row_to_its_order() -> None:
    convo_id, peer_id = _make_conversation()
    for order in (0, 2, 5):
        _seed_log(convo_id, peer_id, order, _text_payload("x"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    state = _ui_state(convo_id, model)

    assert state.adopt_first_unread_row(1) == 2
    assert state.first_unread == 2
    assert state.adopt_first_unread_row(1) is None


def test_abstract_item_model_contract_is_usable_by_a_view() -> None:
    convo_id, peer_id = _make_conversation()
    _seed_log(convo_id, peer_id, 0, _text_payload("hi"))
    model = ConversationLogModel(convo_id)
    model.set_row_count()
    base: QAbstractItemModel = model
    assert base.roleNames()[ROLE_CHAT_AUTHOR] == b"author"
    assert base.rowCount(QModelIndex()) == 1
