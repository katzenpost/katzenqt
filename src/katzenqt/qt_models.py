from PySide6 import QtCore
from PySide6.QtWidgets import QStyledItemDelegate, QLabel, QStyleOptionViewItem, QMainWindow
from PySide6.QtCore import QFile, QSize, QModelIndex, QPersistentModelIndex, QModelRoleDataSpan, QModelRoleData, Slot, QObject, QByteArray
from PySide6.QtGui import QPainter, QImage, QStandardItem
from PySide6.QtQml import QQmlPropertyMap
from PySide6.QtQuick import QQuickImageProvider

from pydantic import BaseModel, Field
import uuid
from typing import Any, NamedTuple

import cbor2

from sqlmodel import Session, col, select

from . import attachment_images, ordering, persistent

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
ROLE_CHAT_EPOCH_COLOR = 0x109
ROLE_CHAT_EPOCH_BOUNDARY = 0x10A


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


def _sender_epoch_of(payload: bytes) -> "bytes | None":
    """The membership hash the SENDER stamped on a row's payload, normalized
    (sentinel/absent -> None). Used only for epoch-anchored ordering; hostile
    input, so it is decoded defensively and never trusted beyond comparison."""
    if payload[:1] != b"F":
        return None
    body = payload[1:]
    try:
        decoded = cbor2.loads(body)
    except Exception:
        decoded = None
    if isinstance(decoded, dict) and "membership_hash" in decoded:
        mh = decoded.get("membership_hash")
        return ordering.normalize_membership_hash(mh if isinstance(mh, bytes)
                                                  else None)
    try:
        from .models import GroupChatMessage
        gm = GroupChatMessage.from_cbor(body)
    except Exception:
        return None
    return ordering.normalize_membership_hash(gm.membership_hash)


def _introduction_read_cap(payload: bytes) -> "bytes | None":
    """The read cap an INTRODUCTION row announces (the member it adds), or None
    for any other row. Same defensive framing as the other payload decoders."""
    if payload[:1] != b"F":
        return None
    try:
        from .models import GroupChatMessage, GroupChatTypeEnum
        gm = GroupChatMessage.from_cbor(payload[1:])
    except Exception:
        return None
    if gm.msg_type != GroupChatTypeEnum.INTRODUCTION or gm.introduction is None:
        return None
    cap: bytes = gm.introduction.read_cap
    return cap


def arrival_membership_states(conversation_id: int) -> "dict[str, bytes]":
    """Map each message id to the LOCAL membership hash in effect when it
    arrived, reconstructed by replaying INTRODUCTION rows in conversation_order
    (a member counts from the row that announced it; members learned at join
    count from the start). Purely local -- no wire field, leaks no reading
    progress -- so it is the colour source, unlike the sender-stamped hash.
    Reconstructs local membership at each message."""
    from . import models
    states: "dict[str, bytes]" = {}
    with Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, conversation_id)
        if conv is None:
            return states
        own_cap = b""
        wcw = sess.get(persistent.WriteCapWAL, conv.write_cap)
        if wcw is not None and wcw.write_cap is not None:
            own_cap = wcw.write_cap[32:]
        peer_caps: list[bytes] = []
        peers = sess.exec(
            select(persistent.ConversationPeer)
            .where(persistent.ConversationPeer.id
                   == persistent.ConversationPeerLink.conversation_peer_id)
            .where(persistent.ConversationPeerLink.conversation_id
                   == conversation_id)
        ).all()
        for peer in peers:
            if peer.id == conv.own_peer_id or not peer.active:
                continue
            if peer.name.startswith(models.SUBSTREAM_NAME_PREFIX):
                continue
            rcw = sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is not None and rcw.read_cap is not None:
                peer_caps.append(rcw.read_cap)
        rows = sess.exec(
            select(persistent.ConversationLog)
            .where(persistent.ConversationLog.conversation_id == conversation_id)
            .order_by(col(persistent.ConversationLog.conversation_order))
        ).all()
        joined_at: "dict[bytes, int]" = {}
        for row in rows:
            cap = _introduction_read_cap(row.payload)
            if cap is not None and cap not in joined_at:
                joined_at[cap] = row.conversation_order
        for row in rows:
            k = row.conversation_order
            caps = [own_cap] if own_cap else []
            for cap in peer_caps:
                if joined_at.get(cap, 0) <= k:
                    caps.append(cap)
            states[str(row.id)] = models.canonical_membership_hash(caps)
    return states


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
        def cache_clear():
            # A display index is cached by row, not by the conversation_order
            # it currently maps to; under a non-default ordering strategy
            # that mapping can change (a later message reshuffles it), so
            # the cache must be dropped whenever the order/epoch caches are,
            # not just left to evict by size.
            cached_func.cache_clear()
            indices_with_stable_network_status.clear()
        wrapper.cache_clear = cache_clear
        wrapper.cache_info = cached_func.cache_info
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
        self.row_count: int = 0
        self._order_cache: "list[int] | None" = None
        self._order_cache_count: int = -1
        self._epoch_cache: "dict[int, ordering.EpochRow] | None" = None
        self._epoch_cache_count: int = -1

    def _message_metas(self) -> "list[ordering.MessageMeta]":
        """Every row's ordering + colouring metadata for this conversation.
        arrival_epoch is the receiver-local membership hash (colour source);
        sender_epoch is the wire-stamped hash (epoch-anchored ordering)."""
        arrival = arrival_membership_states(self.convo_id)
        metas: "list[ordering.MessageMeta]" = []
        with persistent.Session(persistent._engine_sync) as sess:
            rows = sess.query(persistent.ConversationLog).filter(
                persistent.ConversationLog.conversation_id == self.convo_id
            ).all()
            for row in rows:
                peer = row.conversation_peer
                message_id = str(row.id)
                metas.append(ordering.MessageMeta(
                    conversation_order=row.conversation_order,
                    peer_id=row.conversation_peer_id,
                    author=peer.name if peer is not None else "",
                    message_id=message_id,
                    arrival_epoch=arrival.get(message_id),
                    sender_epoch=_sender_epoch_of(row.payload),
                ))
        return metas

    def _epoch_annotations(self) -> "dict[int, ordering.EpochRow]":
        """conversation_order -> EpochRow (arrival colour + divider boundary),
        computed over the active display order and cached against row_count."""
        if (self._epoch_cache is not None
                and self._epoch_cache_count == self.row_count):
            return self._epoch_cache
        metas = self._message_metas()
        order = ordering.active_strategy().order(metas)
        by_co = {m.conversation_order: m for m in metas}
        ordered = [by_co[co] for co in order if co in by_co]
        rows = ordering.annotate_epochs(ordered)
        self._epoch_cache = {r.conversation_order: r for r in rows}
        self._epoch_cache_count = self.row_count
        return self._epoch_cache

    def _display_order(self) -> "list[int] | None":
        """conversation_order values in display position, or None for the
        identity map (the insertion default -- no DB pass, no behaviour change).
        Cached against row_count so a non-default strategy pays one pass per
        change, not one per rendered row."""
        strat = ordering.active_strategy()
        if strat.name == "insertion":
            return None
        if (self._order_cache is not None
                and self._order_cache_count == self.row_count):
            return self._order_cache
        self._order_cache = strat.order(self._message_metas())
        self._order_cache_count = self.row_count
        return self._order_cache

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
            ROLE_CHAT_EPOCH_COLOR: QByteArray(b'epoch_color'),
            ROLE_CHAT_EPOCH_BOUNDARY: QByteArray(b'epoch_boundary'),
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
        self._order_cache = None
        self._epoch_cache = None
        self.data.cache_clear()
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
            ROLE_CHAT_EPOCH_COLOR,
            ROLE_CHAT_EPOCH_BOUNDARY,
        ):
            return None
        index_row : int = index.row()
        #print("DATA: INDEX ROW IS", index_row, repr(index))
        order = self._display_order()
        if order is None:
            target = index_row
        elif 0 <= index_row < len(order):
            target = order[index_row]
        else:
            return None

        if role == ROLE_CHAT_EPOCH_COLOR:
            er = self._epoch_annotations().get(target)
            return er.color if er is not None else None
        if role == ROLE_CHAT_EPOCH_BOUNDARY:
            er = self._epoch_annotations().get(target)
            return bool(er.is_boundary) if er is not None else False

        with persistent.Session(persistent._engine_sync) as sess:
                cl = sess.query(persistent.ConversationLog).filter(
                    persistent.ConversationLog.conversation_id == self.convo_id).filter(
                        persistent.ConversationLog.conversation_order==target).first()
                if cl is None:
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

    async def update_first_unread(self, new_first_unread:int) -> None:
        """Update first_unread in the persistent database:"""
        if new_first_unread == self.first_unread:
            return
        print("UPDATED FIRST_UNREAD", self.first_unread, new_first_unread)
        self.first_unread = new_first_unread
        async with persistent.asession() as sess:
            co = await sess.get(persistent.Conversation, self.conversation_id)
            co.first_unread = self.first_unread
            sess.add(co)
            await sess.commit()
