from PySide6 import QtCore
from PySide6.QtWidgets import QStyledItemDelegate, QLabel, QStyleOptionViewItem, QMainWindow
from PySide6.QtCore import QFile, QSize, QModelIndex, QPersistentModelIndex, QModelRoleDataSpan, QModelRoleData, Slot, QObject, QByteArray
from PySide6.QtGui import QPainter, QImage, QStandardItem
from PySide6.QtQml import QQmlPropertyMap
from PySide6.QtQuick import QQuickImageProvider

from pydantic import BaseModel, Field
from sqlmodel import select
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

_TRANSFER_ROLES = {
    ROLE_TRANSFER_RCW_ID: QByteArray(b"transfer_rcw_id"),
    ROLE_TRANSFER_CONV_ID: QByteArray(b"transfer_conv_id"),
    ROLE_TRANSFER_PARENT_NAME: QByteArray(b"transfer_parent_name"),
    ROLE_TRANSFER_PIECES: QByteArray(b"transfer_pieces"),
    ROLE_TRANSFER_TOTAL: QByteArray(b"transfer_total"),
    ROLE_TRANSFER_ACTIVE: QByteArray(b"transfer_active"),
    ROLE_TRANSFER_FAILED: QByteArray(b"transfer_failed"),
    ROLE_TRANSFER_FAILURE_REASON: QByteArray(b"transfer_failure_reason"),
}


class DownloadsModel(QtCore.QAbstractTableModel):
    """Rows of in-progress/resumable substream file transfers.

    Backs the Transfers QTableView. Columns: Contact, Progress, State, with
    the substream's ReadCapWAL id carried as ROLE_TRANSFER_RCW_ID for the
    Pause/Resume/Remove actions. Rows are added/updated by MainWindow's
    transfers_listener (network.substream_progress_queue) and seeded from
    the database at startup by seed_from_db(). Failed transfers stay visible
    until dismissed by the user.
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
        return 3

    def headerData(self, section: int, orientation: "QtCore.Qt.Orientation", role: int = 0) -> object:  # type: ignore[override]
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation != QtCore.Qt.Orientation.Horizontal:
            return None
        return ("Contact", "Progress", "State")[section]

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
                return "Paused" if not row.get("active", True) else "Downloading"
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
        return None

    # -- mutations (Qt-listener thread) ------------------------------------

    def start_transfer(self, rcw_id: uuid.UUID, conversation_id, parent_name, total) -> None:
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
        }
        self._order.append(rcw_id)
        self.endInsertRows()

    def notify_piece(self, rcw_id: uuid.UUID, pieces) -> None:
        if rcw_id not in self._rows:
            return
        self._rows[rcw_id]["pieces"] = pieces
        row = self._idx(rcw_id)
        idx0 = self.index(row, 1)
        self.dataChanged.emit(idx0, idx0, [ROLE_TRANSFER_PIECES])

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
            self._rows[rcw_id]["failed"] = False
            self._rows[rcw_id].pop("failure_reason", None)
        row = self._idx(rcw_id)
        idx0 = self.index(row, 2)
        self.dataChanged.emit(idx0, idx0, [
            QtCore.Qt.ItemDataRole.DisplayRole, ROLE_TRANSFER_ACTIVE,
            ROLE_TRANSFER_FAILED, ROLE_TRANSFER_FAILURE_REASON,
        ])

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
        self.dataChanged.emit(idx2, idx2, [
            QtCore.Qt.ItemDataRole.DisplayRole, ROLE_TRANSFER_ACTIVE,
            ROLE_TRANSFER_FAILED, ROLE_TRANSFER_FAILURE_REASON,
        ])

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

        A substream is resumable when its peer is active, paused, failed, or
        has ReceivedPiece rows. The Transfers panel keeps those transfers
        visible across a GUI restart.

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
                recv_count = sess.exec(
                    select(persistent.sa.func.count()).select_from(persistent.ReceivedPiece)
                    .where(persistent.ReceivedPiece.read_cap == rcw.id)
                ).one()
                parent = _substream_parent_name(sess, cp)
                if (cp.active or rcw.read_paused or rcw.substream_failure
                        or int(recv_count)):
                    self.start_transfer(
                        rcw.id, cp.conversation.id, parent,
                        rcw.substream_total_chunks,
                    )
                    if int(recv_count):
                        self.notify_piece(rcw.id, int(recv_count))
                    if rcw.read_paused or not cp.active:
                        self.set_paused(rcw.id, paused=True)
                    if rcw.substream_failure is not None:
                        self.fail_transfer(rcw.id, rcw.substream_failure)


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
        }

    @lru_cache(maxsize=10000)
    def index(self, row:int, column:int, parent:QModelIndex | None) -> QModelIndex:
        if parent and parent.isValid():
            return QModelIndex()
        qmi = self.createIndex(row,column, id=row*(column+1))
        return qmi

    @lru_cache(maxsize=10000)
    def parent(self, child:QModelIndex|QPersistentModelIndex) -> QModelIndex:
        """Since we don't have any trees here, nochild indices have parents"""
        return QModelIndex()
    def rowCount(self, parent:QModelIndex|None) -> int:
        """number of chat messages.
        we set this initially when loading in add_conversation(),
        and then each time we receive a message
        or write a message ourselves.
        """
        if not parent or parent.row() == -1:
            return self.row_count
        return 0

    def increment_row_count(self):
        qmi = QModelIndex()
        self.beginInsertRows(qmi, self.row_count-1, self.row_count-1)
        self.row_count += 1
        self.endInsertRows()

    def redraw_network_status(self):
        """Force the view to refresh without actually changing anything."""
        qmi = QModelIndex()
        self.beginInsertRows(qmi, 1,0)
        self.endInsertRows()

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
        ):
            return None
        index_row : int = index.row()
        #print("DATA: INDEX ROW IS", index_row, repr(index))
        # TODO we definitely want to paginate this stuff for performance reasons,
        # and when we do we want order by:
        # sa_relationship_kwargs={"order_by": "conversation_order", "lazy": "dynamic"},

        with persistent.Session(persistent._engine_sync) as sess:
                cl = sess.exec(
                    select(persistent.ConversationLog).where(
                        persistent.ConversationLog.conversation_id == self.convo_id,
                        persistent.ConversationLog.conversation_order == index_row,
                    )
                ).first()
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
    # (projected) ConversationLog.conversation_order of first message the user
    # hasn't "read" yet - it doesn't have to exist in ConversationLog yet.

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
