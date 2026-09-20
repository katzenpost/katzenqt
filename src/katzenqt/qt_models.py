from PySide6 import QtCore
from PySide6.QtWidgets import QStyledItemDelegate, QLabel, QStyleOptionViewItem, QMainWindow
from PySide6.QtCore import QFile, QSize, QModelIndex, QPersistentModelIndex, QModelRoleDataSpan, QModelRoleData, Slot, QObject, QByteArray
from PySide6.QtGui import QPainter, QImage, QStandardItem
from PySide6.QtQml import QQmlPropertyMap
from PySide6.QtQuick import QQuickImageProvider

from pydantic import BaseModel, Field
from sqlmodel import select
import time
import uuid
from typing import Any, NamedTuple

import cbor2

from . import attachment_images, persistent

import functools
from functools import lru_cache

# should probably look into paginating some SQL here so we don't do one query per line
# but err lru_cache makes it bearable for now, with the assumption chat messages don't change.
# TODO, but seems reasonable as long as we are not doing "sent yesterday" etc.

class FilterProxyModel(QtCore.QSortFilterProxyModel):
    # recursiveFilteringEnabled show parents when child matches
    def __index__(self):
        super(FilterProxyModel, self).__index__(self)
    def __init__(self, window: "QMainWindow"):
        self.window = window
        super().__init__(recursiveFilteringEnabled=False)
        # self.setAutoAcceptChildRows(True)

    def invalidate(self):
        super().invalidate()
        self.window.ui.contacts_treeWidget.expandAll()  # ensure we expand all expandable after filtering.

    def filterAcceptsRow(self, source_row, qmi:QModelIndex | QPersistentModelIndex):
        model = self.sourceModel()
        source_idx = model.index(source_row, 0, qmi)
        filterstr = self.window.ui.contactFilterLineEdit.text().lower()
        if filterstr in source_idx.data():
            return True
        parent = source_idx.parent()
        if parent.isValid():
            return filterstr in parent.data()
        return any((self.filterAcceptsRow(child_row, source_idx) for child_row in range(model.rowCount(source_idx))))

ROLE_CHAT_AUTHOR = 0x100  # see ConversationLogModel.roleNames()
ROLE_CHAT_NETWORK_STATUS = 0x101  # ConversationLog.network_status
ROLE_CHAT_MESSAGE_ID = 0x102
ROLE_CHAT_ATTACHMENT_BASENAME = 0x103
ROLE_CHAT_ATTACHMENT_FILETYPE = 0x104
ROLE_CHAT_IS_AUDIO_MESSAGE = 0x105
ROLE_CHAT_ATTACHMENT_KIND = 0x106  # QML: attachment_kind, drives Play/Open/Save visibility
ROLE_CHAT_ATTACHMENT_REL_PATH = 0x107  # QML: attachment_rel_path, spilled file (received only)
ROLE_CHAT_PICTURE_PATH = 0x108  # QML: picture_path, thumbnail rel_path for image attachments
ROLE_CHAT_TALLY_KIND = 0x109  # QML: tally_kind, one of create/vote/recast/close/sync/invalid (tally rows only)
ROLE_CHAT_TALLY_SURVEY_ID = 0x10A  # QML: tally_survey_id, survey id hex to open on click (tally rows only)
ROLE_CHAT_IS_TALLY = 0x10B  # QML: is_tally, true for tally rows (so the delegate can style/click them)

# Custom roles for the Transfers panel. The table is driven by DownloadsModel
# below; these roles let a future delegate/QML entry fetch the
# structured pieces/total rather than parsing the display text.
ROLE_TRANSFER_RCW_ID = 0x200
ROLE_TRANSFER_CONV_ID = 0x201
ROLE_TRANSFER_PARENT_NAME = 0x202
ROLE_TRANSFER_PIECES = 0x203
ROLE_TRANSFER_TOTAL = 0x204
ROLE_TRANSFER_ACTIVE = 0x205  # True = downloading, False = paused
ROLE_TRANSFER_FAILED = 0x206  # True = failed, False/missing = active or paused
ROLE_TRANSFER_FAILURE_REASON = 0x207  # reason string for failed transfers
ROLE_TRANSFER_DIRECTION = 0x208  # "upload" or "download"
ROLE_TRANSFER_RATE = 0x209  # bytes/sec over active transfer time

_TRANSFER_ROLES = {
    ROLE_TRANSFER_RCW_ID: QByteArray(b"transfer_rcw_id"),
    ROLE_TRANSFER_CONV_ID: QByteArray(b"transfer_conv_id"),
    ROLE_TRANSFER_PARENT_NAME: QByteArray(b"transfer_parent_name"),
    ROLE_TRANSFER_PIECES: QByteArray(b"transfer_pieces"),
    ROLE_TRANSFER_TOTAL: QByteArray(b"transfer_total"),
    ROLE_TRANSFER_ACTIVE: QByteArray(b"transfer_active"),
    ROLE_TRANSFER_FAILED: QByteArray(b"transfer_failed"),
    ROLE_TRANSFER_FAILURE_REASON: QByteArray(b"transfer_failure_reason"),
    ROLE_TRANSFER_DIRECTION: QByteArray(b"transfer_direction"),
    ROLE_TRANSFER_RATE: QByteArray(b"transfer_rate"),
}


def format_rate(bytes_per_second: float) -> str:
    """Human-readable transfer rate, e.g. ``1.2 MiB/s``."""
    value = max(bytes_per_second, 0.0)
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if value < 1024:
            if unit == "B/s":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB/s"


class DownloadsModel(QtCore.QAbstractTableModel):
    """Rows of in-progress/resumable substream file transfers.

    Backs the Transfers QTableView. Columns: Contact, Progress, State, with
    the substream's ReadCapWAL id carried as ROLE_TRANSFER_RCW_ID for the
    Pause/Resume/Cancel actions, and ROLE_TRANSFER_DIRECTION distinguishing an
    upload ("upload", keyed by the indirection ReadCapWAL) from a download
    ("download", keyed by the substream ReadCapWAL). Rows are added/updated by
    MainWindow's transfers_listener (network.substream_progress_queue) and
    seeded from the database at startup by seed_from_db(). Failed transfers
    stay visible until dismissed by the user.
    """

    def roleNames(self) -> dict[int, QByteArray]:
        return _TRANSFER_ROLES

    def __init__(self) -> None:
        super().__init__()
        self._rows: "dict[uuid.UUID, dict[str, object]]" = {}
        self._order: "list[uuid.UUID]" = []

    def rowCount(self, parent: "QtCore.QModelIndex | QPersistentModelIndex | None" = None) -> int:  # type: ignore[override]
        if parent is not None and parent.isValid():
            return 0
        return len(self._order)

    def columnCount(self, parent: "QtCore.QModelIndex | QPersistentModelIndex | None" = None) -> int:  # type: ignore[override]
        return 4

    def headerData(self, section: int, orientation: "QtCore.Qt.Orientation", role: int = 0) -> object:  # type: ignore[override]
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation != QtCore.Qt.Orientation.Horizontal:
            return None
        return ("Contact", "Progress", "State", "Rate")[section]

    def data(self, index: "QtCore.QModelIndex", role: int = 0) -> object:  # type: ignore[override]
        if not index.isValid() or not (0 <= index.row() < len(self._order)):
            return None
        rcw_id = self._order[index.row()]
        row = self._rows[rcw_id]
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.EditRole):
            if index.column() == 0:
                return row.get("parent_name")
            if index.column() == 1:
                pieces = int(row.get("pieces", 0))
                total = row.get("total")
                if total is None:
                    return f"{pieces} pieces"
                return f"{pieces}/{total}"
            if index.column() == 2:
                if row.get("failed", False):
                    return f"Failed: {row.get('failure_reason', 'unknown')}"
                if not row.get("active", True):
                    return "Paused"
                return "Uploading" if row.get("direction") == "upload" else "Downloading"
            if index.column() == 3:
                return self._rate_text(row)
            return None
        if role == ROLE_TRANSFER_RCW_ID:
            return str(rcw_id)
        if role == ROLE_TRANSFER_CONV_ID:
            return row.get("conversation_id")
        if role == ROLE_TRANSFER_PARENT_NAME:
            return row.get("parent_name")
        if role == ROLE_TRANSFER_PIECES:
            return row.get("pieces", 0)
        if role == ROLE_TRANSFER_TOTAL:
            return row.get("total")
        if role == ROLE_TRANSFER_ACTIVE:
            return row.get("active", True)
        if role == ROLE_TRANSFER_FAILED:
            return row.get("failed", False)
        if role == ROLE_TRANSFER_FAILURE_REASON:
            return row.get("failure_reason")
        if role == ROLE_TRANSFER_DIRECTION:
            return row.get("direction", "download")
        if role == ROLE_TRANSFER_RATE:
            return self._rate_text(row)
        return None

    def _rate_text(self, row: dict) -> str:
        """Average effective-payload rate over the row's active transfer time.

        Paused or failed rows show zero. The baseline is reset when the row
        first appears and again on each unpause, so the rate is "since the
        transfer (re)started", not since the row was created.
        """
        if row.get("failed", False) or not row.get("active", True):
            return "0 B/s"
        elapsed = time.monotonic() - float(row.get("rate_started_at", 0.0))
        if elapsed < 1.0:
            return "—"
        raw = int(row.get("raw_bytes", 0))
        base = int(row.get("rate_base", 0))
        if row.get("direction") == "upload":
            transferred = base - raw
        else:
            transferred = raw - base
        return format_rate(max(transferred, 0) / elapsed)

    # -- mutations (Qt-listener thread) ------------------------------------

    def start_transfer(self, rcw_id: uuid.UUID, conversation_id, parent_name, total,
                       direction: str = "download", raw_bytes: int = 0) -> None:
        if rcw_id in self._rows:
            # Already tracked; refresh the denominator if it became known.
            if total is not None:
                self._rows[rcw_id]["total"] = total
                row = self._idx(rcw_id)
                idx0 = self.index(row, 1)
                self.dataChanged.emit(idx0, idx0)
            return
        self.beginInsertRows(QtCore.QModelIndex(), len(self._order), len(self._order))
        self._rows[rcw_id] = {
            "conversation_id": conversation_id,
            "parent_name": parent_name,
            "pieces": 0,
            "total": total,
            "active": True,
            "direction": direction,
            # Rate state: raw_bytes is the latest absolute metric (bytes
            # received for a download, bytes still to send for an upload);
            # rate_base is the value the current rate interval started from.
            "raw_bytes": int(raw_bytes),
            "rate_base": int(raw_bytes),
            "rate_started_at": time.monotonic(),
        }
        self._order.append(rcw_id)
        self.endInsertRows()

    def notify_piece(self, rcw_id: uuid.UUID, pieces, raw_bytes=None) -> None:
        if rcw_id not in self._rows:
            return
        self._rows[rcw_id]["pieces"] = pieces
        if raw_bytes is not None:
            self._rows[rcw_id]["raw_bytes"] = int(raw_bytes)
        row = self._idx(rcw_id)
        # Progress and Rate both derive from the new piece, so repaint both.
        idx0 = self.index(row, 1)
        idx1 = self.index(row, 3)
        self.dataChanged.emit(
            idx0, idx1,
            [QtCore.Qt.ItemDataRole.DisplayRole, ROLE_TRANSFER_PIECES,
             ROLE_TRANSFER_RATE],
        )

    def complete_transfer(self, rcw_id: uuid.UUID) -> None:
        if rcw_id not in self._rows:
            return
        row = self._idx(rcw_id)
        self.beginRemoveRows(QtCore.QModelIndex(), row, row)
        del self._rows[rcw_id]
        del self._order[row]
        self.endRemoveRows()

    def set_paused(self, rcw_id: uuid.UUID, paused: bool) -> None:
        if rcw_id not in self._rows:
            return
        self._rows[rcw_id]["active"] = not paused
        if not paused:
            # Unpause resets the rate interval: the next rate is measured from
            # the byte count at resume, not since the transfer first appeared.
            self._rows[rcw_id]["rate_base"] = int(
                self._rows[rcw_id].get("raw_bytes", 0)
            )
            self._rows[rcw_id]["rate_started_at"] = time.monotonic()
            self._rows[rcw_id]["failed"] = False
            self._rows[rcw_id].pop("failure_reason", None)
        row = self._idx(rcw_id)
        idx0 = self.index(row, 2)
        idx1 = self.index(row, 3)
        self.dataChanged.emit(
            idx0, idx1,
            [
                QtCore.Qt.ItemDataRole.DisplayRole,
                ROLE_TRANSFER_ACTIVE,
                ROLE_TRANSFER_FAILED,
                ROLE_TRANSFER_FAILURE_REASON,
                ROLE_TRANSFER_RATE,
            ],
        )

    def fail_transfer(self, rcw_id: uuid.UUID, reason: str) -> None:
        """Mark a transfer as failed with a reason string.

        The row stays visible in the Transfers panel so the user can see
        what failed and why. Use remove_transfer() to dismiss it.

        The caller removes persisted transfer state before dismissing it.
        """
        if rcw_id not in self._rows:
            return
        self._rows[rcw_id]["failed"] = True
        self._rows[rcw_id]["failure_reason"] = reason
        self._rows[rcw_id]["active"] = False  # no longer downloading
        row = self._idx(rcw_id)
        idx2 = self.index(row, 2)  # State column
        idx3 = self.index(row, 3)  # Rate column (forced to 0 B/s)
        self.dataChanged.emit(
            idx2, idx3,
            [
                QtCore.Qt.ItemDataRole.DisplayRole,
                ROLE_TRANSFER_ACTIVE,
                ROLE_TRANSFER_FAILED,
                ROLE_TRANSFER_FAILURE_REASON,
                ROLE_TRANSFER_RATE,
            ],
        )

    def remove_transfer(self, rcw_id: uuid.UUID) -> None:
        """Remove a transfer row from the model (user-dismissal of failed/complete)."""
        if rcw_id not in self._rows:
            return
        row = self._idx(rcw_id)
        self.beginRemoveRows(QtCore.QModelIndex(), row, row)
        del self._rows[rcw_id]
        del self._order[row]
        self.endRemoveRows()

    def _idx(self, rcw_id: uuid.UUID) -> int:
        return self._order.index(rcw_id)

    # -- startup seeding ----------------------------------------------------

    def seed_from_db(self) -> None:
        """Populate rows for resumable substream transfers already on disk.

        A substream is resumable when its peer is still active (currently
        reading) *or* it has ReceivedPiece rows (paused mid-transfer). The
        Transfers panel is where substream transfers are paused/resumed, so
        this seeding keeps the panel populated across a GUI restart.

        Sync engine: this runs on the Qt loop and builds Qt model rows, so it
        neither opens the async engine (see persistent.warm_async_engine) nor
        hands the model to the io loop.
        """
        with persistent.Session(persistent._engine_sync) as sess:
            from . import network
            prefix = network._SUBSTREAM_NAME_PREFIX
            streams = sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.name.like(f"{prefix}%"),
                )
            ).all()
            for cp in streams:
                rcw = sess.get(persistent.ReadCapWAL, cp.read_cap_id)
                if rcw is None:
                    continue
                recv_count, recv_bytes = sess.exec(
                    select(
                        persistent.sa.func.count(),
                        persistent.sa.func.coalesce(
                            persistent.sa.func.sum(
                                persistent.sa.func.length(
                                    persistent.ReceivedPiece.chunk,
                                ),
                            ),
                            0,
                        ),
                    ).select_from(persistent.ReceivedPiece)
                    .where(persistent.ReceivedPiece.read_cap == rcw.id)
                ).one()
                parent = _substream_parent_name(sess, cp)
                # Resumable = active, or received something but not yet
                # assembled to the terminal F (still has pieces outstanding).
                if cp.active or int(recv_count):
                    # Rate counts from the on-disk byte count at seed, so a
                    # transfer resumed across a relaunch starts at zero.
                    self.start_transfer(
                        rcw.id, cp.conversation.id, parent,
                        rcw.substream_total_chunks, raw_bytes=int(recv_bytes),
                    )
                    if int(recv_count):
                        self.notify_piece(
                            rcw.id, int(recv_count), int(recv_bytes),
                        )
                    if not cp.active:
                        self.set_paused(rcw.id, paused=True)

            self._seed_uploads(sess)

    def _seed_uploads(self, sess) -> None:
        """Seed Transfers rows for outbound substreams still in flight.

        An in-flight upload is a gated I-chunk: a PlaintextWAL with a non-null
        ``indirection`` whose target ReadCapWAL carries the chunk total. It has
        no ConversationPeer row, so it is seeded separately. A substream with
        no remaining C/F PWALs has already finished (the I-chunk is now
        dispatchable), so it gets no row; the chat bubble covers the wait for
        the I-chunk's own ACK.
        """
        i_chunks = sess.exec(
            select(persistent.PlaintextWAL).where(
                persistent.PlaintextWAL.indirection != None,  # noqa: E711
            )
        ).all()
        for i_chunk in i_chunks:
            rcw = sess.get(persistent.ReadCapWAL, i_chunk.indirection)
            if rcw is None or rcw.write_cap_id is None:
                continue
            remaining, remaining_bytes = sess.exec(
                select(
                    persistent.sa.func.count(),
                    persistent.sa.func.coalesce(
                        persistent.sa.func.sum(
                            persistent.sa.func.length(
                                persistent.PlaintextWAL.bacap_payload,
                            ) - 1,
                        ),
                        0,
                    ),
                ).select_from(persistent.PlaintextWAL)
                .where(persistent.PlaintextWAL.bacap_stream == rcw.write_cap_id)
            ).one()
            remaining = int(remaining)
            remaining_bytes = int(remaining_bytes)
            if remaining <= 0:
                continue
            conv = sess.get(persistent.Conversation, i_chunk.conversation_id)
            total = rcw.substream_total_chunks
            basename = _upload_basename_for_agg(sess, rcw.id)
            label = (
                f"{basename} (in {conv.name})"
                if basename and conv is not None
                else (conv.name if conv is not None else "")
            )
            # Rate counts from the bytes still outstanding at seed, so a
            # transfer resumed across a relaunch starts at zero.
            self.start_transfer(
                rcw.id, i_chunk.conversation_id, label,
                total, direction="upload", raw_bytes=remaining_bytes,
            )
            if total is not None:
                self.notify_piece(rcw.id, total - remaining, remaining_bytes)
            agg_wcw = sess.get(persistent.WriteCapWAL, rcw.write_cap_id)
            if agg_wcw is not None and agg_wcw.paused:
                self.set_paused(rcw.id, paused=True)


def _substream_parent_name(sess, cp) -> str:
    """Best-effort display name of a substream peer's parent, for the panel.
    ``sess`` is a sync ``persistent.Session`` (the Qt-loop read path)."""
    from .network import _substream_parent_id
    parent_id = _substream_parent_id(cp.name)
    parent = (
        sess.get(persistent.ConversationPeer, parent_id)
        if parent_id is not None else None
    )
    if parent is not None:
        return parent.name
    # Fall back to the conversation name; the substream peer itself is
    # synthetic and must never surface.
    conv = sess.get(persistent.Conversation, cp.conversation.id)
    return conv.name if conv is not None else cp.name


PACKET_COLUMNS = (
    "Sent", "Dir", "Kind", "Stream", "Pos", "Status", "Retry", "In flight",
    "Timeout in",
)

_PACKET_STATUS_LABELS = {
    "in_flight": "In flight",
    "payload": "Payload",
    "empty": "Empty",
    "boxnotfound": "Box not found",
    "acked": "ACKed",
    "timed_out": "Timed out",
    "link_down": "Link down",
    "cancelled": "Cancelled",
    "error": "Error",
}


class PacketsModel(QtCore.QAbstractTableModel):
    """Table of per-packet records from ``network.packets_snapshot()``.

    Stream labels and substream totals are resolved lazily from the sync
    engine and cached per stream id. The Packets dialog drives ``refresh()`` on
    a timer.
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: "list[dict]" = []
        self._ids: "list[str]" = []
        self._stream_info: "dict[object, tuple[str, int | None]]" = {}

    def rowCount(self, parent=None) -> int:  # type: ignore[override]
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=None) -> int:  # type: ignore[override]
        return len(PACKET_COLUMNS)

    def headerData(self, section, orientation, role=0):  # type: ignore[override]
        if (role == QtCore.Qt.ItemDataRole.DisplayRole
                and orientation == QtCore.Qt.Orientation.Horizontal):
            return PACKET_COLUMNS[section]
        return None

    def data(self, index, role=0):  # type: ignore[override]
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        if role not in (QtCore.Qt.ItemDataRole.DisplayRole,
                        QtCore.Qt.ItemDataRole.EditRole):
            return None
        return self._cell(self._rows[index.row()], index.column())

    def refresh(self) -> None:
        from . import network
        snapshot = network.packets_snapshot()
        in_flight = network.PACKET_STATUS_IN_FLIGHT
        # In-flight first (oldest first, nearest to timeout), then finished
        # newest first.
        snapshot.sort(key=lambda r: (
            r["status"] != in_flight,
            r["sent_at"] if r["status"] == in_flight
            else -(r["finished_at"] or 0.0),
        ))
        new_ids = [r["id"] for r in snapshot]
        self._rows = snapshot
        if new_ids != self._ids:
            self._ids = new_ids
            self.beginResetModel()
            self.endResetModel()
        elif self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, len(PACKET_COLUMNS) - 1),
            )

    def _cell(self, row, column):
        from . import network
        if column == 0:
            return time.strftime("%H:%M:%S", time.localtime(row["sent_wall"]))
        if column == 1:
            return "Read" if row["kind"].endswith("_read") else "Write"
        if column == 2:
            return row["kind"].replace("_", " ")
        if column == 3:
            return self._stream_info_for(row)[0]
        if column == 4:
            position = row["box_position"]
            if position is None:
                position = row["box_index"]
            if position is None:
                return "—"
            total = self._stream_info_for(row)[1]
            if total:
                return f"{position}/{total}"
            return str(position)
        if column == 5:
            return _PACKET_STATUS_LABELS.get(row["status"], row["status"])
        if column == 6:
            return str(max(int(row.get("attempt", 1)) - 1, 0))
        if column == 7:
            end = row["finished_at"]
            if end is None:
                end = time.monotonic()
            return network.format_duration(end - row["sent_at"])
        if column == 8:
            if row["finished_at"] is not None or row["timeout_s"] is None:
                return "—"
            remaining = row["sent_at"] + row["timeout_s"] - time.monotonic()
            if remaining <= 0:
                return "overdue"
            return network.format_duration(remaining)
        return None

    def _stream_info_for(self, row) -> "tuple[str, int | None]":
        kind = row["kind"]
        if kind.startswith("voucher"):
            return (row.get("stage") or "voucher", None)
        stream_id = row["stream_id"]
        if stream_id is None:
            return ("—", None)
        info = self._stream_info.get(stream_id)
        if info is None:
            info = self._query_stream_info(stream_id)
            self._stream_info[stream_id] = info
        label, total = info
        # A label captured at send time survives the I-chunk's deletion, which
        # breaks the DB link once the upload has been ACK'd.
        if row.get("label"):
            label = row["label"]
        return (label, total)

    def _query_stream_info(self, stream_id) -> "tuple[str, int | None]":
        """(label, substream_total_chunks) for a stream id.

        The total is set only for file-transfer substreams, on the substream's
        own ReadCapWAL (a read) or its indirection ReadCapWAL (an agg write).
        """
        from .network import _SUBSTREAM_NAME_PREFIX, _substream_parent_id
        with persistent.Session(persistent._engine_sync) as sess:
            cp = sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.read_cap_id == stream_id,
                )
            ).first()
            if cp is not None:
                if cp.name.startswith(_SUBSTREAM_NAME_PREFIX):
                    rcw = sess.get(persistent.ReadCapWAL, stream_id)
                    total = rcw.substream_total_chunks if rcw is not None else None
                    parent_id = _substream_parent_id(cp.name)
                    parent = (
                        sess.get(persistent.ConversationPeer, parent_id)
                        if parent_id is not None else None
                    )
                    if parent is not None:
                        return (f"substream of {parent.name}", total)
                    return ("substream", total)
                return (cp.name, None)
            conv = sess.exec(
                select(persistent.Conversation).where(
                    persistent.Conversation.write_cap == stream_id,
                )
            ).first()
            if conv is not None:
                own = (
                    sess.get(persistent.ConversationPeer, conv.own_peer_id)
                    if conv.own_peer_id is not None else None
                )
                return (f"{own.name if own is not None else 'you'} in {conv.name}", None)
            rcw = sess.exec(
                select(persistent.ReadCapWAL).where(
                    persistent.ReadCapWAL.write_cap_id == stream_id,
                )
            ).first()
            if rcw is not None:
                total = rcw.substream_total_chunks
                pwal = sess.exec(
                    select(persistent.PlaintextWAL).where(
                        persistent.PlaintextWAL.bacap_stream == stream_id,
                    )
                ).first()
                if pwal is not None:
                    conv = sess.get(
                        persistent.Conversation, pwal.conversation_id,
                    )
                    if conv is not None:
                        basename = _upload_basename_for_agg(sess, rcw.id)
                        if basename:
                            return (f"{basename} (in {conv.name})", total)
                        return (f"substream of {conv.name}", total)
                return ("substream", total)
        return (str(stream_id)[:8], None)


def _upload_basename_for_agg(sess, rcw_id) -> "str | None":
    """The filename of an outbound substream, from its ConversationLog marker.

    Resolvable only while the upload is in progress: the agg stream links to
    the log row through the I-chunk (``PlaintextWAL.indirection`` ->
    ``ConversationLog.outgoing_pwal``), and the I-chunk is deleted once ACK'd.
    """
    i_chunk = sess.exec(
        select(persistent.PlaintextWAL).where(
            persistent.PlaintextWAL.indirection == rcw_id,
        )
    ).first()
    if i_chunk is None:
        return None
    convlog = sess.exec(
        select(persistent.ConversationLog).where(
            persistent.ConversationLog.outgoing_pwal == i_chunk.id,
        )
    ).first()
    if convlog is None:
        return None
    info = _decode_group_chat_payload(convlog.payload)
    return info.basename if info.kind == "outgoing" else None


class AttachmentDisplay(NamedTuple):
    """Renderer-friendly view of a ConversationLog.payload.

    ``kind`` is one of ``"text"``, ``"inline"``, ``"marker"``, ``"outgoing"``
    or ``"oversized"`` and drives which (if any) attachment controls QML shows.
    ``picture_path`` is a state-dir-relative thumbnail path for image
    attachments (and ``None`` otherwise), consumed by ChatImageProvider.
    """
    display: str
    basename: str | None
    filetype: str | None
    is_audio: bool
    kind: str  # text | inline | marker | outgoing | oversized
    rel_path: str | None
    picture_path: str | None = None


def _attachment_display_for_marker(decoded: dict[str, Any]) -> AttachmentDisplay:
    """Build an :class:`AttachmentDisplay` from a decoded CBOR marker dict
    (``file_marker`` / ``file_outgoing`` / ``file_oversized``)."""
    kind = decoded.get("kind")
    basename = decoded.get("basename") or "unnamed"
    filetype = decoded.get("filetype")
    is_audio = filetype == "audio/opus"

    if kind == "file_oversized":
        size = decoded.get("size") or 0
        mib = size / (1024 * 1024)
        return AttachmentDisplay(
            display=f"[attachment too large] {basename} ({mib:.1f} MiB)",
            basename=basename,
            filetype=filetype,
            is_audio=False,
            kind="oversized",
            rel_path=None,
        )

    is_image = attachment_images.is_image_attachment(filetype, basename)
    rel_path = decoded.get("rel_path")  # received marker only
    # Image rows render as a thumbnail, so suppress the redundant
    # "[attachment] test.jpg" text. The thumbnail falls back to the full
    # received file when no dedicated thumb was generated.
    if is_image:
        display = ""
        picture_path = decoded.get("thumb_rel_path") or rel_path
    else:
        display = (
            f"Voice note: {basename}" if is_audio else f"[attachment] {basename}"
        )
        picture_path = None

    if kind == "file_outgoing":
        # src_path is never surfaced to QML; the resolve helper reads it from
        # the persisted payload on demand.
        return AttachmentDisplay(
            display=display,
            basename=basename,
            filetype=filetype,
            is_audio=is_audio,
            kind="outgoing",
            rel_path=None,
            picture_path=picture_path,
        )
    # file_marker (received)
    return AttachmentDisplay(
        display=display,
        basename=basename,
        filetype=filetype,
        is_audio=is_audio,
        kind="marker",
        rel_path=rel_path,
        picture_path=picture_path,
    )


# Cache decoded rows so scrolling does not re-parse CBOR on every repaint.
# Keyed on payload bytes: modern rows carry a small marker (well under a KiB),
# so 512 entries is roughly half a MiB; only deprecated inline rows hold a full
# image, and those are no longer produced.
_DECODE_CACHE_SIZE = 512


@lru_cache(maxsize=_DECODE_CACHE_SIZE)
def _decode_group_chat_payload(payload: bytes) -> AttachmentDisplay:
    # Keep ConversationLog as the source of truth and derive renderer-friendly
    # roles lazily so audio rows can share the same persistence format as text.
    from .models import clamp_message_text

    if payload[:1] != b"F":
        # Pre-protocol rows: raw UTF-8 text, no CBOR wrapper.
        return AttachmentDisplay(
            clamp_message_text(payload.decode(errors="replace")),
            None, None, False, "text", None,
        )

    body = payload[1:]

    # Attachment markers (received/sent/oversized) are CBOR dicts carrying a
    # "kind" key; try that before the inline GroupChatMessage decode.
    try:
        decoded = cbor2.loads(body)
    except Exception:
        decoded = None
    if isinstance(decoded, dict) and decoded.get("kind") in (
        "file_marker", "file_outgoing", "file_oversized",
    ):
        return _attachment_display_for_marker(decoded)

    try:
        from .models import GroupChatMessage

        # Older rows store the full GroupChatMessage CBOR inline (bytes in payload).
        group_message = GroupChatMessage.from_cbor(body)
    except Exception:
        return AttachmentDisplay(
            clamp_message_text(payload.decode(errors="replace")),
            None, None, False, "text", None,
        )

    if group_message.text:
        return AttachmentDisplay(
            clamp_message_text(group_message.text), None, None, False, "text", None,
        )

    if group_message.file_upload is not None:
        basename = group_message.file_upload.basename
        filetype = group_message.file_upload.filetype
        is_audio_message = filetype == "audio/opus"
        # Legacy inline images have no spilled thumbnail; the resolve path
        # rehydrates the full bytes to a cache file on demand, so leave
        # picture_path unset here and rely on the attachment controls.
        if attachment_images.is_image_attachment(filetype, basename):
            display = ""
        elif is_audio_message:
            display = f"Voice note: {basename}"
        else:
            display = f"[attachment] {basename}"
        return AttachmentDisplay(
            display, basename, filetype, is_audio_message, "inline", None,
        )

    return AttachmentDisplay("", None, None, False, "text", None)


def _attachment_role_value(info: AttachmentDisplay, role: object) -> object:
    """Map a decoded payload to the value for one attachment/display role."""
    if role == 0:
        return info.display
    if role == ROLE_CHAT_ATTACHMENT_BASENAME:
        return info.basename
    if role == ROLE_CHAT_ATTACHMENT_FILETYPE:
        return info.filetype
    if role == ROLE_CHAT_IS_AUDIO_MESSAGE:
        return info.is_audio
    if role == ROLE_CHAT_ATTACHMENT_KIND:
        return info.kind
    if role == ROLE_CHAT_ATTACHMENT_REL_PATH:
        return info.rel_path
    if role == ROLE_CHAT_PICTURE_PATH:
        return info.picture_path
    return None


def _is_tally_type(msg_type) -> bool:
    """True for the tally protocol's message family."""
    from .models import GroupChatTypeEnum as T

    return msg_type in (
        T.TALLY_CREATE, T.TALLY_VOTE, T.TALLY_CLOSE,
        T.TALLY_SYNC_REQ, T.TALLY_SYNC_RESP,
    )


_TALLY_ROW_CACHE: "dict[tuple, object]" = {}


def _tally_row(cl):
    """The projected :class:`presenter.TallyRowText` for a tally log row, or
    None when the row is not a tally message. Cached by row id: a log row's
    payload and peer name do not change."""
    if cl.payload[:1] != b"F":
        return None
    key = (str(cl.id), cl.conversation_peer.name if cl.conversation_peer else "")
    cached = _TALLY_ROW_CACHE.get(key, False)
    if cached is not False:
        return cached
    from .models import GroupChatMessage
    from .tally import presenter

    try:
        gcm = GroupChatMessage.from_cbor(cl.payload[1:])
    except Exception:
        return None
    if getattr(gcm, "tally", None) is None and not _is_tally_type(gcm.msg_type):
        return None
    summary = None
    if gcm.tally is not None:
        summary = _tally_survey_summary(cl.conversation_id, gcm.tally.survey_id)
    row = presenter.tally_row_text(
        gcm,
        actor_name=cl.conversation_peer.name if cl.conversation_peer else "?",
        survey_summary=summary,
    )
    _TALLY_ROW_CACHE[key] = row
    return row


def _tally_survey_summary(conversation_id: int, survey_id: bytes):
    """Project the persisted survey a tally row concerns, or None if absent."""
    from .tally import presenter
    from .tally.sync import load_doc

    blob = presenter.survey_doc(conversation_id, survey_id)
    if blob is None:
        return None
    try:
        doc = load_doc(blob)
    except ValueError:
        return None
    return presenter.summarize(
        doc, conversation_id=conversation_id,
        voter_names=presenter.voter_names(conversation_id),
    )


def lru_cache_for_data_roles(maxsize=10000):
    """decorator for QtCore.QAbstractItemModel.data() that exempts certain roles (network status for unsent)"""
    def decorator(func):
        cached_func = lru_cache(maxsize=maxsize)(func)
        indices_with_stable_network_status = dict()
        @functools.wraps(func)
        def wrapper(clm:"ConversationLogModel", index:QModelIndex, role:QtCore.Qt.ItemDataRole|None):
            if role != ROLE_CHAT_NETWORK_STATUS:
                return cached_func(clm, index, role) ## call item.data() and cache it
            elif (status := indices_with_stable_network_status.get(index, None)) is not None:
                return status
            else:
                ret = func(clm, index, role)
                if ret != 1:  # received or sent, but not "pending"
                    indices_with_stable_network_status[index] = ret
                return ret
        # Expose a cache_clear so a model reset (row removal) can drop stale
        # cells: the value cache indexes by QModelIndex, and the stable-status
        # map keys by index too.
        def cache_clear() -> None:
            cached_func.cache_clear()
            indices_with_stable_network_status.clear()
        wrapper.cache_clear = cache_clear
        return wrapper
    return decorator

class ConversationLogModel(QtCore.QAbstractItemModel):
    # https://doc.qt.io/qt-6/model-view-programming.html
    # https://doc.qt.io/qt-6/qt.html#ItemDataRole-enum

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QAbstractItemModel.html
    # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QStandardItemModel.html

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QModelIndex.html
    # QModelIndex index = model->index(row, column, parent);

    def __init__(self, convo_id) -> None:
        super().__init__()
        self.convo_id = convo_id
        # The ordered list of conversation_order values is cached and re-read
        # from the database by refresh_row_count() (called on a
        # conversation-update notification). Deriving it from the log, rather
        # than incrementing a counter at each writer, means a writer that
        # forgets to notify cannot desync the view permanently: the next
        # notification re-reads the truth. Row ``r`` renders the log row with
        # ``conversation_order == _orders[r]``, which tolerates gaps left by a
        # deleted (cancelled) message; ``_row_count`` is the DB truth (what
        # rowCount() returns); ``_view_count`` is how many rows Qt has actually
        # been told about, which drives insert/reset transitions.
        self._orders: list[int] = []
        self._row_count = 0
        self._view_count = 0

    def roleNames(self):
        """These map names used in QML to ints used in QAbstractItemModel
        Inside a DelegateItem you can access model.author.
        See e.g. https://doc.qt.io/archives/qt-6.4/qt.html#ItemDataRole-enum
        """
        return {
            0: QByteArray(b'display'),
            #4: QByteArray(b'statusTip'),
            #1: QByteArray(b'decoration'),
            #2: QByteArray(b'edit'),
            #5: QByteArray(b'whatsThis'),
            #3: QByteArray(b'toolTip'),
            ROLE_CHAT_AUTHOR: QByteArray(b'author'),
            ROLE_CHAT_NETWORK_STATUS: QByteArray(b'network_status'),
            ROLE_CHAT_MESSAGE_ID: QByteArray(b'message_id'),
            ROLE_CHAT_ATTACHMENT_BASENAME: QByteArray(b'attachment_basename'),
            ROLE_CHAT_ATTACHMENT_FILETYPE: QByteArray(b'attachment_filetype'),
            ROLE_CHAT_IS_AUDIO_MESSAGE: QByteArray(b'is_audio_message'),
            ROLE_CHAT_ATTACHMENT_KIND: QByteArray(b'attachment_kind'),
            ROLE_CHAT_ATTACHMENT_REL_PATH: QByteArray(b'attachment_rel_path'),
            ROLE_CHAT_PICTURE_PATH: QByteArray(b'picture_path'),
            ROLE_CHAT_TALLY_KIND: QByteArray(b'tally_kind'),
            ROLE_CHAT_TALLY_SURVEY_ID: QByteArray(b'tally_survey_id'),
            ROLE_CHAT_IS_TALLY: QByteArray(b'is_tally'),
        }

    def index(self, row:int, column:int, parent:QModelIndex | None) -> QModelIndex:
        """A standard flat-list index: no custom internal id, and out-of-range
        rows are invalid. QML's TreeView adapts this model through
        QQmlTreeModelToTableModel, which stores QPersistentModelIndexes; an
        index identity derived from the row (and cached) desyncs that adapter
        across inserts and crashes it."""
        if parent and parent.isValid():
            return QModelIndex()
        if row < 0 or row >= self._row_count or column < 0:
            return QModelIndex()
        return self.createIndex(row, column)

    def parent(self, child:QModelIndex|QPersistentModelIndex) -> QModelIndex:
        """Since we don't have any trees here, nochild indices have parents"""
        return QModelIndex()
    def rowCount(self, parent:QModelIndex|None) -> int:
        """number of chat messages, from the cached DB count (see
        refresh_row_count). Qt calls this during layout/paint, so it must not
        query; the cache is refreshed on each conversation-update notification.
        """
        if not parent or parent.row() == -1:
            return self._row_count
        return 0

    def _query_orders(self) -> list[int]:
        """The conversation's conversation_order values, ascending.

        Enumerating the actual orders (rather than assuming
        ``index_row == conversation_order``) keeps rows addressable after a
        deletion leaves a gap."""
        with persistent.Session(persistent._engine_sync) as sess:
            return list(sess.exec(
                select(persistent.ConversationLog.conversation_order)
                .where(
                    persistent.ConversationLog.conversation_id == self.convo_id
                )
                .order_by(persistent.ConversationLog.conversation_order)
            ))

    def set_row_count(self, count: int) -> None:
        """Seed the cached count without emitting signals (startup, when the
        view has no rows yet). Both the DB-truth count and the count Qt has
        been told about start equal, so the first later insert uses a range the
        view can accept."""
        self._orders = self._query_orders()
        self._row_count = int(count)
        self._view_count = int(count)
        self._clear_data_caches()

    def refresh_row_count(self) -> None:
        """Re-read the log's orders and reconcile the view.

        Transitions are driven by ``_view_count`` (rows Qt has actually been
        told about), never by the raw DB count: seeding the count at startup
        without an insert means the view's bookkeeping can lag the model's, and
        emitting a range computed from the DB count then crashes the view.
        Growth whose existing prefix is unchanged inserts the missing tail; any
        other change (a deletion left a gap, or a reorder) resets the model,
        because a shifted index invalidates cached cells. Because the orders
        come from the log, a writer that appends a row without notifying this
        model self-heals on the next notification instead of desyncing the view
        permanently.
        """
        new_orders = self._query_orders()
        if new_orders == self._orders:
            # No change: repaint in place (no transition).
            self.redraw_network_status()
            return
        new_count = len(new_orders)
        prefix_unchanged = (
            new_count > self._view_count
            and new_orders[:self._view_count] == self._orders[:self._view_count]
        )
        if prefix_unchanged:
            first = self._view_count
            qmi = QModelIndex()
            self.beginInsertRows(qmi, first, new_count - 1)
            self._orders = new_orders
            self._row_count = new_count
            self._view_count = new_count
            self.endInsertRows()
            return
        # Shrink, gap, or reorder: reset rather than compute a delta, so any
        # shifted indices and stale cached cells are dropped wholesale. Clear
        # the caches before the transition, not between begin/end.
        self._clear_data_caches()
        self.beginResetModel()
        self._orders = new_orders
        self._row_count = new_count
        self._view_count = new_count
        self.endResetModel()

    def redraw_network_status(self):
        """Repaint the network-status column without changing the row set."""
        if self._view_count == 0:
            return
        top = self.index(0, 0, QModelIndex())
        bottom = self.index(self._view_count - 1, 0, QModelIndex())
        self.dataChanged.emit(top, bottom, [ROLE_CHAT_NETWORK_STATUS])

    def _clear_data_caches(self) -> None:
        """Drop cached data()/tally-row cells after the row count changed.

        ``data()`` is lru-cached per (model, index, role) and tally rows are
        cached per row id; a reset (deletion) can shift rows and invalidate
        both, so any count change clears them.
        """
        clear = getattr(self.data, "cache_clear", None)
        if clear is not None:
            clear()
        _TALLY_ROW_CACHE.clear()

    def refresh_tally_rows(self) -> None:
        """Re-project every row after a tally event.

        A tally row's text depends on whether the survey it names is known and
        on that survey's current state: an early vote renders as "unknown poll"
        until the create arrives, and a create's placeholder gains its vote
        counts. Both are cached by row id, so drop the caches and repaint all
        roles."""
        self._clear_data_caches()
        if self._view_count == 0:
            return
        self.dataChanged.emit(
            self.index(0, 0, QModelIndex()),
            self.index(self._view_count - 1, 0, QModelIndex()),
            [],
        )

    def columnCount(self, parent:QModelIndex|QPersistentModelIndex|None) -> int:
        if parent.isValid():
            return 0
        return 1

    @lru_cache_for_data_roles()
    def data(self, index:QModelIndex, role:QtCore.Qt.ItemDataRole|None):
        """returns data for index
        PySide6.QtCore.Qt.DisplayRole
        """
        if role not in (
            0,
            ROLE_CHAT_AUTHOR,
            ROLE_CHAT_NETWORK_STATUS,
            ROLE_CHAT_MESSAGE_ID,
            ROLE_CHAT_ATTACHMENT_BASENAME,
            ROLE_CHAT_ATTACHMENT_FILETYPE,
            ROLE_CHAT_IS_AUDIO_MESSAGE,
            ROLE_CHAT_ATTACHMENT_KIND,
            ROLE_CHAT_ATTACHMENT_REL_PATH,
            ROLE_CHAT_PICTURE_PATH,
            ROLE_CHAT_TALLY_KIND,
            ROLE_CHAT_TALLY_SURVEY_ID,
            ROLE_CHAT_IS_TALLY,
        ):
            return None
        index_row : int = index.row()
        # Render the log row whose conversation_order is at this position. The
        # cached order list is authoritative after a refresh; before the first
        # refresh (a bare createIndex in tests) fall back to identity.
        order = (
            self._orders[index_row]
            if index_row < len(self._orders) else index_row
        )
        #print("DATA: INDEX ROW IS", index_row, repr(index))
        # TODO we definitely want to paginate this stuff for performance reasons,
        # and when we do we want order by:
        # sa_relationship_kwargs={"order_by": "conversation_order", "lazy": "dynamic"},
        #
        # TODO (2026-09-20) DB-chatter reductions, not urgent:
        #   - data() opens a Session and runs one indexed SELECT per uncached
        #     (index, role); a fast scroll over unseen rows can burst many
        #     one-query sessions. A per-conversation in-model row cache keyed
        #     by conversation_order (invalidated on insert/reset) would remove
        #     the per-paint queries.
        #   - redraw_network_status() emits dataChanged over the whole range on
        #     every conversation notification, making the view re-ask roles for
        #     every row. Narrow it to the row whose status actually changed (the
        #     ACK path) instead.
        #   - refresh_row_count() reads the conversation's full order list per
        #     conversation event (not per scroll/mouse); that cadence is fine,
        #     keep it tied to events. A tail-only query would be cheaper on the
        #     common append path but cannot detect a middle deletion.

        with persistent.Session(persistent._engine_sync) as sess:
                cl = sess.exec(
                    select(persistent.ConversationLog).where(
                        persistent.ConversationLog.conversation_id == self.convo_id,
                        persistent.ConversationLog.conversation_order == order,
                    )
                ).first()
                if cl is None:
                    # The count can briefly outrun the committed rows (or a
                    # reset can race a paint); never deref None for a role.
                    return None
                # TODO we probably want to do this as multiple columns? whatever, works for now
                if role == ROLE_CHAT_AUTHOR:
                    if cl.network_status == 1:
                        return cl.conversation_peer.name
                    return cl.conversation_peer.name
                elif role == ROLE_CHAT_NETWORK_STATUS:
                    return cl.network_status
                elif role == ROLE_CHAT_MESSAGE_ID:
                    return str(cl.id)
                else:
                    tally = _tally_row(cl)
                    if tally is not None:
                        if role == 0:
                            return tally.text
                        if role == ROLE_CHAT_TALLY_KIND:
                            return tally.kind
                        if role == ROLE_CHAT_TALLY_SURVEY_ID:
                            return tally.survey_id.hex() if tally.survey_id else None
                        if role == ROLE_CHAT_IS_TALLY:
                            return True
                        if role == ROLE_CHAT_AUTHOR:
                            return cl.conversation_peer.name
                        # No attachment/picture roles for tally rows.
                        return None
                    # Derive display text and attachment roles from the payload.
                    # INTRODUCTION rows carry no body text, so surface the
                    # announcement ("<author> added <name>") before the
                    # attachment-oriented decode handles the rest.
                    if role == 0 and cl.payload[:1] == b"F":
                        try:
                            from .models import GroupChatMessage
                            cm = GroupChatMessage.from_cbor(cl.payload[1:])
                        except Exception:
                            cm = None
                        if cm is not None and (intro := cm.as_introduction):
                            return (
                                f"{cl.conversation_peer.name} added "
                                f"{intro.display_name}"
                            )
                    info = _decode_group_chat_payload(cl.payload)
                    return _attachment_role_value(info, role)
                # TODO here we want to have a ROLE_CHAT_ACKED to show which of our things have been sent
        #print(self,"data", index, repr(QtCore.Qt.ItemDataRole(role)))
        #return f"hi {self.convo_id}"
    def headerData(self, section:int, orientation:QtCore.Qt.Orientation, role:QtCore.Qt.ItemDataRole|None):
        """data for given role and section in the header"""
        print(self,"headerData",section,orientation,repr(QtCore.Qt.ItemDataRole(role)))
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return "header1"
        return None
        return QLabel("a")
        pass
    def flags(self, index:QModelIndex):
        # https://doc.qt.io/qtforpython-6/PySide6/QtCore/Qt.html#PySide6.QtCore.Qt.ItemFlag
        return QtCore.Qt.NoItemFlags

class ChatImageProvider(QQuickImageProvider):
    """Serves inline chat thumbnails for ``image://ChatImageProvider/<rel>``.

    ``<rel>`` is a state-dir-relative path produced by
    ``attachment_images.spill_image_thumbnail`` (a small JPEG) or, for
    legacy rows without a dedicated thumbnail, the full received image.
    Missing or undecodable files yield a null image, which QML renders as
    an empty (hidden) row picture rather than an error."""
    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.Image)  # type: ignore[attr-defined]

    def requestImage(self, path: str, size: QtCore.QSize, requestedSize: QtCore.QSize) -> QImage:
        if not path:
            return QImage()
        abs_path = persistent.state_file.parent / path
        # Guard against path traversal escaping the state directory.
        try:
            abs_path.resolve().relative_to(persistent.state_file.parent.resolve())
        except ValueError:
            return QImage()
        img = attachment_images.load_bounded_image(abs_path)
        if img is None:
            return QImage()
        # Full images (legacy fallback) are scaled to the thumbnail box so
        # rows stay compact; pre-sized thumbnails pass through unchanged.
        max_px = attachment_images.THUMB_MAX_PX
        if img.width() > max_px or img.height() > max_px:
            img = img.scaled(
                max_px, max_px,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        if size is not None:
            size.setWidth(img.width())
            size.setHeight(img.height())
        return img


class ConversationUIState(BaseModel):
    """Per-conversation runtime state used by the Qt UI.

    Lives in qt_models.py rather than models.py because its fields
    (ConversationLogModel, QStandardItem, QQmlPropertyMap) are Qt types
    and forcing every importer of `models` to load PySide6 broke
    headless consumers (the integration runner, pytest collection
    against a runner without libEGL).
    """
    model_config = {
        'validate_assignment': True,
        'arbitrary_types_allowed': True
    }
    conversation_id : int
    own_peer_id : int = Field(description="ConversationPeer.id for self")
    own_peer_name : str = Field(description="ConversationPeer.name for self")
    own_peer_bacap_uuid: uuid.UUID
    chat_lineEdit_buffer : str
    conversation_log_model: ConversationLogModel
    contacts_standard_item : QStandardItem = Field(description="the entry in the Contacts pane for the conversation")
    chat_lines_scroll_idx : float = 0.0
    # TODO: should store scroll state of self.ui.ChatLines
    # ie self.ui.ChatLines.scrollToBottom() for default new ones
    last_push_to_talk_ns : int = 0
    attached_files : set[str] = Field(default_factory=set)

    first_unread : int = 0
    # ConversationLog.conversation_order of the first message the user hasn't
    # "read" yet. QML's marker walks visible rows and the timeline is exactly
    # the conversation log (tally messages are ordinary rows), so this is both
    # the row index and the order.

    def qml_ctx(self, rootObject:QObject|None, settings:dict[str,str|int|None]) -> QQmlPropertyMap:
        props = QQmlPropertyMap(rootObject)
        props.insert({
            **settings,
            "chatTreeViewModel": self.conversation_log_model,
            "conversation_scroll": self.chat_lines_scroll_idx,
            "first_unread": self.first_unread,
            "chat_text_size": 11, # governs text size of chat messages
            "contact_name_text_size": settings.get("contactName.font.pointSize", 11), # governs text size of contact names
        })
        return props

    def mark_first_unread(self, new_first_unread:int) -> bool:
        """Set the in-memory first_unread cursor; return True if it changed.

        The persistent write is kept single-writer on the io loop: callers
        follow up with network.persist_first_unread() only when this returns
        True."""
        if new_first_unread == self.first_unread:
            return False
        print("UPDATED FIRST_UNREAD", self.first_unread, new_first_unread)
        self.first_unread = new_first_unread
        return True
