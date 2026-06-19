APP_ORGANIZATION = "Mixnetwork"
APP_NAME = "KatzenQt"

import argparse
import asyncio
import fcntl
import hashlib
import logging
import math
import os
import shutil
import sys
import threading
import time
import uuid
from asyncio import ensure_future
from pathlib import Path
from typing import NamedTuple, TYPE_CHECKING

import cbor2

import PySide6.QtAsyncio as QtAsyncio
#from PySide6.QtCore.GObject.QtTest import QAbstractItemModelTester
from PySide6 import QtCore, QtNetwork
from PySide6.QtCore import (QCoreApplication, QEvent, QFile, QModelIndex,
                            QSettings, QSize, Slot, QThread, QUrl, Signal,
                            QTimer)
from PySide6.QtGui import (QAction, QDesktopServices, QIcon, QKeySequence,
                           QPixmap, QShortcut, QStandardItem, QStandardItemModel)
from PySide6.QtQml import QQmlNetworkAccessManagerFactory, QQmlPropertyMap
from PySide6.QtTest import QAbstractItemModelTester
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox,
                               QFileDialog, QFontDialog, QInputDialog, QLabel,
                               QFormLayout, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QStyle, QSystemTrayIcon,
                               QTextBrowser, QToolButton, QTreeView,
                               QTreeWidgetItem, QVBoxLayout)
from sqlalchemy import func
from sqlmodel import select

# https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html
from . import network  # this is network.py
from . import persistent
from . import theme  # theme.py: light/dark/system theming
from base64 import b64decode, b64encode
from .voucher import (await_and_open, cancel_pending_voucher,
                     conversation_is_joined, derive_read_and_induct,
                     list_pending_vouchers, mint_and_publish,
                     pending_voucher_for)
from .audio_ptt import AudioEngineError, AudioEngineUnavailable, PttAudioBridge
from .katzen_util import create_task
from .models import (GroupChatFileUpload,
                     GroupChatMessage, GroupChatPleaseAdd, SendOperation)
#from ui_mixchat_chatview import Ui_ChatForm
# qt_models.py — also re-exports ConversationUIState (moved here so the
# headless `models` module can stay PySide6-free).
from .qt_models import *
from .ui_font_settings import Ui_FontSettingsDialog  # ui_font_settings.py
from .ui_mixchat import Ui_MainWindow  # ui_mixchat.py

if TYPE_CHECKING:
    import typing
    from typing import Dict, List

logger = logging.getLogger("katzen")
logger.setLevel("INFO")


class _AttachmentError(Exception):
    """User-facing attachment problem (missing file, checksum mismatch,
    oversized body that was dropped on receive)."""


class _ResolvedAttachment(NamedTuple):
    """On-disk path for a chat attachment, looked up from ConversationLog."""
    basename: str
    filetype: str | None
    path: Path

class AsyncioThread(threading.Thread):
    def run(self):
        self.loop = asyncio.new_event_loop()
        def report_exception3(loop, exception):
            if 'exception' in exception:
                if isinstance(exception['exception'], asyncio.CancelledError):
                    return
            print("AsyncioThread exception", exception)
        self.loop.set_exception_handler(report_exception3)
        self.loop.run_until_complete(self.async_main())
        self.loop.run_until_complete(network.start_background_threads(self.kp_client))

    async def run_in_io(self, fn):
        """Run (fn) in the io loop, to work around QtAsyncio not providing sock_connect etc.

        Would be nice to have a "with" context handler I guess.

        """
        ffff = asyncio.run_coroutine_threadsafe(fn, self.loop)
        res = await asyncio.wrap_future(ffff)
        assert ffff.exception() is None
        assert ffff.result() == res
        return res

    async def async_main(self):
        self.kp_client = None
        while self.kp_client is None:
            try:
                self.kp_client = await network.reconnect()
            except (ConnectionRefusedError,BrokenPipeError) as e:
                print("Failed to connect:", e)
                import traceback; traceback.print_exc()
                await asyncio.sleep(1)

# matplotlib in qt:
# https://www.datacamp.com/tutorial/introduction-to-pyside6-for-building-gui-applications-with-python

# https://doc.qt.io/qtforpython-6/PySide6/QtQml/QQmlEngine.html
# offlineStoragePathᅟ - The directory for storing offline user data
# clearComponentCache()

def todo_unicode():
    # for unicode entry:
    qks = QKeySequence(tr("Ctrl-Shift-u"))
    # if we bind an event for this, and then push a mode that recognizes
    # [0-9a-fA-F]
    # and pops that state when encountering another one, we can do
    # poor man's IME unicode input

def todo_keys_push_to_talk():
    # probably a listener on main window
    # that checks if the input tab open is push to talk
    # QEvent::KeyPress, QEvent::KeyRelease
    # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QKeyEvent.html
    # The event handlers QWidget::keyPressEvent(), QWidget::keyReleaseEvent(), QGraphicsItem::keyPressEvent() and QGraphicsItem::keyReleaseEvent() receive key events.
    pass

def todo_keys():
    # QWidgetLineControl::processShortcutOverrideEvent(QKeyEvent *ke) - maybe for shift-enter in single-line chat?
    # for searching in chats:
    find = QKeySequence(QKeySequence.StandardKey.Find)
    findNext = QKeySequence(QKeySequence.StandardKey.FindNext)
    findPrev = QKeySequence(QKeySequence.StandardKey.FindNextPrevious)
    # for pg up/down in chat view:
    pgUp = QKeySequence(QKeySequence.StandardKey.MoveToNextPage)
    pgDown = QKeySequence(QKeySequence.StandardKey.MoveToPreviousPage)
    ctrlHome = QKeySequence(QKeySequence.StandardKey.MoveToEndOfDocument)
    # for switching contacts:
    shiftPgUp = QKeySequence(QKeySequence.StandardKey.SelectNextPage)
    shiftPgDown = QKeySequence(QKeySequence.StandardKey.SelectPreviousPage)

def async_cb(method):
    """this wraps async MainWindow methods and calls them """
    def f(*args, **kwargs):
        return ensure_future(method(*args, **kwargs))
    return f


class FirewallNetworkAccessManager(QtNetwork.QNetworkAccessManager):
    def connectToHost(self, *args) -> None:
        print("firewall: connectToHost")
        return
    def connectToHostEncrypted(self, *args) -> None:
        print("firewall: connectToHostEncrypted")
        return
class FirewallNetworkAccessManagerFactory(QQmlNetworkAccessManagerFactory):
    def create(self, obj:QObject) -> QtNetwork.QNetworkAccessManager:
        """TODO not sure this actually works"""
        return FirewallNetworkAccessManager(obj)

class KeyPressFilter(QObject):
    """Listen for Space(bar) / push-to-talk"""
    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.KeyPress:
            text = event.text()
            #if event.modifiers():
            #    text = event.keyCombination().key().name.decode(encoding="utf-8")
            print("ev", text)
        else:
            print(event.type(), repr(event))
        return False

def duration_time_ns():
    """Portable nanosecond delta timestamps.

    This is ~SI nanoseconds, but platform-defined epoch instead of UNIX epoch.
    """
    try:
        # Linux-specific: On Linux CLOCK_MONOTONIC adds drift to mitigate skew,
        # but they later added _RAW which doesn't:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
    except AttributeError:
        # BSDs use SI seconds by default:
        return time.monotonic_ns()

class PendingVouchersDialog(QDialog):
    """Lists in-flight vouchers and lets the user abandon stale ones, e.g. a
    voucher whose code was lost and whose join never completed."""
    def __init__(self, parent, rows):
        super().__init__(parent)
        self.setWindowTitle("Pending vouchers")
        # ids the user marked for abandonment; the caller deletes them once the
        # dialog closes. Scheduling the deletion here, while exec()'s nested event
        # loop runs, would re-enter QtAsyncio's task machinery and crash.
        self.cancelled = []
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Vouchers that have not finished joining. Cancel one to abandon it; "
            "you may then generate a fresh voucher for that conversation."
        ))
        self.list_widget = QListWidget()
        for pv_id, conv_name, role, step in rows:
            item = QListWidgetItem(f"{conv_name}   [{role}, {step}]")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, pv_id)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)
        cancel_btn = QPushButton("Cancel selected voucher")
        cancel_btn.clicked.connect(self._cancel_selected)
        layout.addWidget(cancel_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _cancel_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        self.cancelled.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self.list_widget.takeItem(self.list_widget.row(item))


class MainWindow(QMainWindow):
    def X_keyPressEvent(self, ev: "QEvent") -> None:
        key = ev.key()  # type: ignore[attr-defined]
        print("key pressed", key)
    def X_keyReleaseEvent(self, ev: "QEvent") -> None:
        key = ev.key()  # type: ignore[attr-defined]
        print("key released", key)

    def _push_to_talk_audio(self) -> PttAudioBridge | None:
        if getattr(self, "_ptt_audio_failed", False):
            return None
        if audio := getattr(self, "_ptt_audio", None):
            return audio
        try:
            self._ptt_audio = PttAudioBridge()
        except AudioEngineUnavailable as exc:
            self._ptt_audio_failed = True
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self,
                    APP_NAME,
                    str(exc),
                ),
            )
            return None
        # The attachment controls are created before the audio engine is lazily
        # initialized, so refresh them once the backend becomes available.
        if hasattr(self, "stop_attachment_audio_button"):
            self._update_attachment_controls()
        return self._ptt_audio

    def _push_to_talk_reset_ui(self) -> None:
        self.ui.ptt_hold_space_label.setText("Hold space bar to record audio.")

    def _show_status_message(
        self,
        title: str,
        message: str,
        *,
        timeout_ms: int = 5000,
    ) -> None:
        status_text = f"{title}: {message}" if title else message
        self.statusBar().showMessage(status_text, timeout_ms)

    def _start_playback_monitor(self, failure_message: str) -> None:
        self._playback_failure_message = failure_message
        self._playback_error_timer.start()

    def _stop_playback_monitor(self) -> None:
        self._playback_failure_message = None
        self._playback_error_timer.stop()

    def _poll_playback_error(self) -> None:
        audio = getattr(self, "_ptt_audio", None)
        if audio is None:
            self._stop_playback_monitor()
            return

        try:
            if error := audio.take_playback_error():
                failure_message = self._playback_failure_message or "Audio playback failed."
                self._stop_playback_monitor()
                QMessageBox.critical(
                    self,
                    f"ERROR: {APP_NAME}",
                    f"{failure_message}\n\n{error}",
                )
                return
            if not audio.is_playing:
                # Playback may finish just before the Rust thread stores an error.
                if error := audio.take_playback_error():
                    failure_message = self._playback_failure_message or "Audio playback failed."
                    self._stop_playback_monitor()
                    QMessageBox.critical(
                        self,
                        f"ERROR: {APP_NAME}",
                        f"{failure_message}\n\n{error}",
                    )
                    return
                self._stop_playback_monitor()
        except AudioEngineError as exc:
            self._stop_playback_monitor()
            QMessageBox.critical(
                self,
                f"ERROR: {APP_NAME}",
                f"Failed to monitor audio playback.\n\n{exc}",
            )

    def _selected_attachment_item(self) -> QListWidgetItem | None:
        return self.ui.attached_files_QListWidget.currentItem()

    def _selected_attachment_path(self) -> Path | None:
        item = self._selected_attachment_item()
        if item is None:
            return None
        path = item.data(0x100)
        if not path:
            return None
        return Path(path)

    def _is_previewable_attachment(self, path: Path | None) -> bool:
        return path is not None and path.is_file() and path.suffix.lower() == ".opus"

    def _update_attachment_controls(self) -> None:
        selected_path = self._selected_attachment_path()
        audio = getattr(self, "_ptt_audio", None)
        is_draft = bool(audio and selected_path and audio.is_draft_path(selected_path))

        self.preview_attachment_button.setEnabled(self._is_previewable_attachment(selected_path))
        self.stop_attachment_audio_button.setEnabled(audio is not None)
        self.discard_attachment_button.setEnabled(selected_path is not None)
        self.discard_attachment_button.setText(
            "Discard voice note" if is_draft else "Remove attachment"
        )

    def _play_attachment_preview(self) -> None:
        audio = self._push_to_talk_audio()
        selected_path = self._selected_attachment_path()
        if not audio or not self._is_previewable_attachment(selected_path):
            return
        try:
            audio.play_preview(selected_path)
            self._start_playback_monitor("Failed to preview the selected audio clip.")
        except AudioEngineError as exc:
            QMessageBox.critical(
                self,
                f"ERROR: {APP_NAME}",
                f"Failed to preview the selected audio clip.\n\n{exc}",
            )

    def _discard_selected_attachment(self) -> None:
        item = self._selected_attachment_item()
        if item is None:
            return
        convo = self.convo_state()
        path = Path(item.data(0x100))
        audio = getattr(self, "_ptt_audio", None)
        if audio is not None:
            try:
                audio.stop_playback()
            except AudioEngineError:
                pass
            self._stop_playback_monitor()
            if audio.is_draft_path(path):
                audio.discard_draft(path)
        convo.attached_files.discard(str(path))
        self.refresh_attached_files_for_conversation(convo)

    def _resolve_attachment(self, message_id: str) -> "_ResolvedAttachment | None":
        """Rehydrate an attachment to a concrete on-disk path.

        QML only carries lightweight model roles, so the payload is decoded
        here on demand. Handles the three persisted shapes:

        * ``file_marker`` (received): the bytes are already spilled to disk by
          :func:`network._spill_attachment`; verify the SHA-256 and return it.
        * ``file_outgoing`` (sent): read from the original ``src_path`` if it
          is still on disk; an empty ``src_path`` (voice-note draft) yields
          ``None``.
        * inline ``GroupChatMessage.file_upload`` (legacy): spill the inline
          bytes into a per-message cache file.

        Raises :class:`_AttachmentError` for oversized markers, missing
        spilled files, and checksum mismatches. ``self`` is intentionally
        unused so the helper can be exercised in isolation.
        """
        try:
            message_uuid = uuid.UUID(message_id)
        except ValueError:
            return None

        with persistent.Session(persistent._engine_sync) as sess:
            conversation_log = sess.get(persistent.ConversationLog, message_uuid)
            if conversation_log is None or conversation_log.payload[:1] != b"F":
                return None
            body = conversation_log.payload[1:]  # strip framing byte shared with wire format

        state_root = persistent.state_file.parent

        # Attachment markers are CBOR dicts carrying a "kind" key.
        try:
            decoded = cbor2.loads(body)
        except Exception:
            decoded = None

        if isinstance(decoded, dict) and "kind" in decoded:
            kind = decoded.get("kind")
            basename = decoded.get("basename") or "unnamed"
            filetype = decoded.get("filetype")

            if kind == "file_oversized":
                raise _AttachmentError(
                    f"{basename} was too large to receive and its contents "
                    "were dropped."
                )

            if kind == "file_marker":
                # Bytes live under state_dir/attachments/; marker holds rel_path + sha256.
                rel_path = decoded.get("rel_path")
                if not rel_path:
                    raise _AttachmentError(f"{basename} has no stored location.")
                abs_path = state_root / rel_path
                if not abs_path.is_file():
                    raise _AttachmentError(
                        f"The received file for {basename} is missing on disk."
                    )
                marker_sha = decoded.get("sha256")
                if marker_sha is not None:
                    actual_sha = hashlib.sha256(abs_path.read_bytes()).digest()
                    if actual_sha != marker_sha:
                        raise _AttachmentError(
                            f"Checksum mismatch for {basename}; the file may be "
                            "corrupt."
                        )
                return _ResolvedAttachment(basename, filetype, abs_path)

            if kind == "file_outgoing":
                src_path = decoded.get("src_path") or ""
                if not src_path:
                    # Voice-note draft was discarded after sending; nothing to
                    # play back until the peer echoes it.
                    return None
                abs_path = Path(src_path)
                if not abs_path.is_file():
                    raise _AttachmentError(
                        f"The original file for {basename} is no longer "
                        f"available at {src_path}."
                    )
                return _ResolvedAttachment(basename, filetype, abs_path)

            return None

        # Legacy inline file upload: spill the bytes to a cache file.
        try:
            group_message = GroupChatMessage.from_cbor(body)
        except Exception:
            return None
        if group_message.file_upload is None:
            return None
        upload = group_message.file_upload
        cache_dir = state_root / "attachments" / "_inline_cache"
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe = network._safe_basename(upload.basename)
        cache_path = cache_dir / f"{message_id}-{safe}"
        if not cache_path.exists() or cache_path.read_bytes() != upload.payload:
            cache_path.write_bytes(upload.payload)
        return _ResolvedAttachment(upload.basename, upload.filetype, cache_path)

    @Slot(str)
    def playReceivedMessage(self, message_id: str) -> None:
        """QML slot: play a received or sent voice note."""
        audio = self._push_to_talk_audio()
        if not audio:
            return
        try:
            resolved = self._resolve_attachment(message_id)
        except _AttachmentError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if resolved is None:
            return
        try:
            # Rust playback wants a stable file path; cache from resolved bytes.
            cached_path = audio.cache_received_clip(
                message_id,
                resolved.basename,
                resolved.path.read_bytes(),
            )
            audio.play_received(cached_path)
            self._start_playback_monitor("Failed to play the received voice note.")
        except AudioEngineError as exc:
            QMessageBox.critical(
                self,
                f"ERROR: {APP_NAME}",
                f"Failed to play the received voice note.\n\n{exc}",
            )

    @Slot(str)
    def openAttachment(self, message_id: str) -> None:
        """QML slot: open a non-audio attachment with the desktop handler."""
        try:
            resolved = self._resolve_attachment(message_id)
        except _AttachmentError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if resolved is None:
            QMessageBox.information(
                self, APP_NAME,
                "This attachment is not available locally yet.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved.path)))

    @Slot(str)
    def saveAttachment(self, message_id: str) -> None:
        """QML slot: copy attachment bytes to a user-chosen path."""
        try:
            resolved = self._resolve_attachment(message_id)
        except _AttachmentError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if resolved is None:
            QMessageBox.information(
                self, APP_NAME,
                "This attachment is not available locally yet.",
            )
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save attachment", resolved.basename,
        )
        if not dest:
            return
        try:
            shutil.copyfile(resolved.path, dest)
        except OSError as exc:
            QMessageBox.critical(
                self, f"ERROR: {APP_NAME}",
                f"Could not save the attachment.\n\n{exc}",
            )

    @Slot()
    def stopAudioPlayback(self) -> None:
        audio = getattr(self, "_ptt_audio", None)
        if audio is None:
            return
        try:
            audio.stop_playback()
            self._stop_playback_monitor()
        except AudioEngineError as exc:
            self._stop_playback_monitor()
            QMessageBox.critical(
                self,
                f"ERROR: {APP_NAME}",
                f"Failed to stop audio playback.\n\n{exc}",
            )

    def push_to_talk_start(self):
        audio = self._push_to_talk_audio()
        if not audio:
            return
        convo = self.convo_state()
        self._stop_playback_monitor()
        try:
            audio.start_capture(convo.conversation_id)
        except AudioEngineError as exc:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.critical(
                    self,
                    f"ERROR: {APP_NAME}",
                    f"Failed to start push-to-talk capture.\n\n{exc}",
                ),
            )
            return

        self.push_to_talk_started = True
        self.push_to_talk_recording_conversation_id = convo.conversation_id
        self.ui.ptt_hold_space_label.setText(
            "Recording audio... release Space to attach the voice note."
        )
        if not self.push_to_talk_watchdog.isActive():
            self.push_to_talk_watchdog.start()

    def push_to_talk_finish(self, *, cancel: bool) -> None:
        if not getattr(self, "push_to_talk_started", False):
            return

        audio = getattr(self, "_ptt_audio", None)
        self.push_to_talk_started = False
        self.push_to_talk_recording_conversation_id = None
        self.push_to_talk_watchdog.stop()
        self._push_to_talk_reset_ui()

        if not audio:
            return

        if cancel:
            try:
                audio.cancel_capture()
            except AudioEngineError as exc:
                QTimer.singleShot(
                    0,
                    lambda: QMessageBox.critical(
                        self,
                        f"ERROR: {APP_NAME}",
                        f"Failed to cancel push-to-talk capture.\n\n{exc}",
                    ),
                )
            return

        try:
            draft = audio.stop_capture()
        except AudioEngineError as exc:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.critical(
                    self,
                    f"ERROR: {APP_NAME}",
                    f"Failed to finalize push-to-talk capture.\n\n{exc}",
                ),
            )
            return

        convo = self.convo_state()
        convo.attached_files.add(str(draft.path))
        self.refresh_attached_files_for_conversation(convo)
        self.ui.singlemultitab.setCurrentWidget(self.ui.attach_file_tab)
        self._show_status_message(
            "Voice note attached",
            f"{draft.path.name} ({draft.duration_seconds:.1f}s)",
        )

    def push_to_talk_watchdog_tick(self) -> None:
        if not getattr(self, "push_to_talk_started", False):
            self.push_to_talk_watchdog.stop()
            return
        try:
            convo = self.convo_state()
        except Exception:
            self.push_to_talk_finish(cancel=True)
            return

        if convo.conversation_id != self.push_to_talk_recording_conversation_id:
            self.push_to_talk_finish(cancel=True)
            return

        if self.ui.ptt_tab.parent().currentWidget() != self.ui.ptt_tab:
            self.push_to_talk_finish(cancel=True)
            return

        if duration_time_ns() - convo.last_push_to_talk_ns > 100_000_000:
            self.push_to_talk_finish(cancel=False)

    def font_settings_dialog(self):
        def font_example(qtoolbtn):
            ok, font = QFontDialog.getFont() # returns a QFont
            if not ok:
                return
            qtoolbtn.setText(f"{font.family()} {font.pointSize()}")
            option = qtoolbtn.objectName().replace("_toolButton", "")
            new = {
              f"{option}.font.family": font.family(),
              f"{option}.font.pointSize": font.pointSize(),
            }
            self.settings = {**self.settings, **new}
            with persistent.Session(persistent._engine_sync) as sess:
                for n,v in new.items():
                  if not (ex := sess.get(persistent.AppSetting, n)):
                      ex = persistent.AppSetting(id=n)
                  ex.type = 'int' if isinstance(v,int) else 'str'
                  ex.value = v
                  sess.add(ex)
                sess.commit()
            # In theory we could now redraw the conversation QML, but we don't have
            # a way to trigger a full redraw. We should have something similar to
            # "on another conversation selected", but which we could force-redraw with.

        if not getattr(self, 'font_settings_qdialog', None):
            q = QDialog()
            u = Ui_FontSettingsDialog()
            u.setupUi(q)
            self.font_settings_qdialog = q # avoid it going out of scope
            for el in q.children():
                if not isinstance(el, QToolButton):
                    continue
                def stupid_scoping(x):
                    return lambda: font_example(x)
                el.clicked.connect(stupid_scoping(el))
                # restore font choice from DB, if any:
                which = el.objectName().replace("_toolButton","")
                if old_text := (self.settings.get(f"{which}.font.family","") + " " + str(self.settings.get(f"{which}.font.pointSize",""))).strip():
                    el.setText(old_text)
        self.font_settings_qdialog.show()

    def theme_settings_dialog(self):
        # Modal chooser; applies and persists on accept (see theme.py).
        theme.ThemeDialog(self.theme, self).exec()

    @async_cb
    async def testme(self):
        logger.info("testing")
        import secrets
        x = secrets.token_bytes(32)
        import base64
        #print(base64.z85encode(x))
        print(base64.b64encode(x))
        write_cap , read_cap = network.create_new_keypair(x)
        logger.critical(write_cap)
        logger.critical(read_cap)
        await self.iothread.run_in_io(network.test_keypair(self.iothread.kp_client, write_cap, read_cap))
        # we want to make a regular conversation,
        # give it a name,
        # pick a name for ourselves
        # set up Conversation + ConversationLog in persistent
        # make RCW + WCW
        # upload the RCW to the deterministic stream via a models.GroupChatPleaseAdd
        # or even better a GroupChatReplyWho(please_adds=...) for the conversation
        
        

    def push_to_talk_pressed(self):
        """The shortcut has autoRepeat=True, so we will keep getting these at regular intervals.
        Instead of relying on receiving a keyReleased event, we do a "dead man's switch" thing
        and record until we haven't seen a PTT shortcut trigger for a while (~100ms? - TODO how
        do we learn the autoRepeat rate?).

        TODO ptt_hold_space_label should be saying "RECORDING AUDIO ..." when we are
          - we should restore the label after
        TODO: when it's saying that we should have a button to cancel.
        TODO: We should also have a keyboard combination to do that, while they're holding space (escape? delete?)
        """
        # CONDITION: push to talk mode in the chat input
        if self.ui.ptt_tab.parent().currentWidget() != self.ui.ptt_tab:
            # Switched away from the ptt tab, we should stop recording.
            # Probably cancel sending it too.
            # TODO actually this could just be a listener on singlemultitab
            # then we can also make the space shortcut conditional there.
            if getattr(self, "push_to_talk_started", False):
                self.push_to_talk_finish(cancel=True)
            return True
        #== "ptt_tab"
        # - conversation not changed
        now_ns = duration_time_ns()
        convo = self.convo_state()
        if now_ns - convo.last_push_to_talk_ns > 100_000_000:
            # if we were recording in a previous tab we should end it
            convo.last_push_to_talk_ns = now_ns
        else:
            # we wait for two events, so a single errant spacebar press doesn't send 50ms of audio
            print("talking")
            convo.last_push_to_talk_ns = duration_time_ns()
            if not getattr(self, "push_to_talk_started", False):
                self.push_to_talk_start()
        return False

    def __init__(self, app : QApplication) -> None:
        self.systray = None
        super(MainWindow, self).__init__()
        self.conversation_state_by_id : Dict[int, ConversationUIState] = dict()
        self.app = app
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        # Expose a tiny playback surface to QML instead of routing the whole
        # window object through custom model roles.
        self.ui.qml_ChatLines.rootContext().setContextProperty("chatController", self)
        self._ptt_audio: PttAudioBridge | None = None
        self._ptt_audio_failed = False
        self._playback_failure_message: str | None = None
        self._playback_error_timer = QTimer(self)
        self._playback_error_timer.setInterval(100)
        self._playback_error_timer.timeout.connect(self._poll_playback_error)
        self.push_to_talk_started = False
        self.push_to_talk_recording_conversation_id: int | None = None
        self.push_to_talk_watchdog = QTimer(self)
        self.push_to_talk_watchdog.setInterval(50)
        self.push_to_talk_watchdog.timeout.connect(self.push_to_talk_watchdog_tick)
        # Override the generated UI defaults: enable the list, allow single
        # selection, and use ListMode so items scroll vertically rather than
        # wrapping as icons.
        self.ui.attached_files_QListWidget.setEnabled(True)
        self.ui.attached_files_QListWidget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.ui.attached_files_QListWidget.setViewMode(
            QListView.ViewMode.ListMode
        )
        # Draft review happens in the existing attachment tab so the send path can
        # stay unchanged once the voice note is finalized.
        self.preview_attachment_button = QToolButton(self.ui.attach_file_tab)
        self.preview_attachment_button.setText("Preview clip")
        self.preview_attachment_button.setIcon(QIcon.fromTheme("media-playback-start"))
        self.preview_attachment_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.preview_attachment_button.setEnabled(False)
        self.ui.formLayout_2.setWidget(
            2,
            QFormLayout.ItemRole.FieldRole,
            self.preview_attachment_button,
        )
        self.stop_attachment_audio_button = QToolButton(self.ui.attach_file_tab)
        self.stop_attachment_audio_button.setText("Stop audio")
        self.stop_attachment_audio_button.setIcon(QIcon.fromTheme("media-playback-stop"))
        self.stop_attachment_audio_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.stop_attachment_audio_button.setEnabled(False)
        self.ui.formLayout_2.setWidget(
            3,
            QFormLayout.ItemRole.FieldRole,
            self.stop_attachment_audio_button,
        )
        self.discard_attachment_button = QToolButton(self.ui.attach_file_tab)
        self.discard_attachment_button.setText("Remove attachment")
        self.discard_attachment_button.setIcon(QIcon.fromTheme("edit-delete"))
        self.discard_attachment_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.discard_attachment_button.setEnabled(False)
        self.ui.formLayout_2.setWidget(
            4,
            QFormLayout.ItemRole.FieldRole,
            self.discard_attachment_button,
        )

        # failed attempt to firewall external resource access:
        eng = self.ui.qml_ChatLines.engine()
        chat_image_provider = ChatImageProvider()
        eng.addImageProvider("ChatImageProvider", chat_image_provider)
        nwa = eng.networkAccessManager()
        #import pdb;pdb.set_trace()
        fnamf = FirewallNetworkAccessManagerFactory()
        a = fnamf.create(QObject())
        eng.setNetworkAccessManagerFactory(fnamf)

        self.setWindowTitle(APP_NAME)
        # self.dumpObjectTree() # display debug

        #self.eventFilter = KeyPressFilter(parent=self)
        #self.installEventFilter(self.eventFilter)

        space = QKeySequence(self.app.tr("Space","Shortcut|Push to talk"))
        qs_ptt = QShortcut(space, self, self.push_to_talk_pressed)

        #import pdb;pdb.set_trace()
        #self.ui.keyPressed.triggered.connect(lambda: print("key down"))
        #self.keyRelease.triggered.connect(lambda: print("key up"))

        # item delegates define custom looks for view items
        self.ui.action_display_font.triggered.connect(self.font_settings_dialog)
        # Theme: enable the (otherwise disabled) menu action, wire its dialog,
        # then restore the persisted light/dark/system choice.
        self.ui.action_theme.setEnabled(True)
        self.ui.action_theme.triggered.connect(self.theme_settings_dialog)
        self.theme = theme.ThemeManager(self.app, self)
        self.theme.restore()
        self.ui.action_testme.triggered.connect(self.testme)
        self.ui.action_space.triggered.connect(self.new_conversation)
        self.ui.action_new_conversation.triggered.connect(self.new_conversation)
        self.ui.action_accept_invitation.triggered.connect(self.induct_via_voucher)
        self.ui.action_invite_contact.triggered.connect(self.generate_voucher)
        self.ui.action_pending_vouchers.triggered.connect(self.show_pending_vouchers)
        # Make the [Quit] toolbar actually quit:
        self.ui.action_quit.triggered.connect(lambda ev: self.close(ev,really_quit=True))

        # Sending files to a conversation: File attachment/sending callbacks
        self.ui.attach_file_button.clicked.connect(self.attach_file)
        self.ui.send_file_button.clicked.connect(self.send_file)
        self.ui.attached_files_QListWidget.itemSelectionChanged.connect(
            self._update_attachment_controls
        )
        self.preview_attachment_button.clicked.connect(self._play_attachment_preview)
        self.stop_attachment_audio_button.clicked.connect(self.stopAudioPlayback)
        self.discard_attachment_button.clicked.connect(self._discard_selected_attachment)

        self.all_contacts = QStandardItemModel()
        #self.ui.contacts_treeWidget.setModel(self.all_contacts)
        fpm = FilterProxyModel(self)
        fpm.setSourceModel(self.all_contacts)
        self.ui.contacts_treeWidget.setModel(fpm)
        # refresh contact filter when the user types in the contact search bar:
        self.ui.contactFilterLineEdit.textChanged.connect(lambda:fpm.invalidate())
        #fpm.setFilterWildcard("*")

        # inputMethodEvent
        #self.ui.contacts_treeWidget.keyboardSearch.connect(lambda: print("KB search")) # TODO not a signal, but when user starts typing here we want to set the focus to contactFilterLineEdit instead
        self.ui.contacts_treeWidget.selectionModel().currentChanged.connect(self.conversation_selected)
        self.ui.chat_lineEdit.returnPressed.connect(self.chat_msg_single_line)

    async def _enqueue_outgoing_gcm(
        self,
        convo_state: "ConversationUIState",
        gcm: GroupChatMessage,
        *,
        local_payload: bytes,
    ) -> None:
        """Shared WAL-commit path for both text and file sends.

        Serialises ``gcm`` into the conversation's own write-cap BACAP stream,
        records the WriteCapWAL/PlaintextWAL rows the network writer consumes,
        appends an optimistic ConversationLog row (so the message shows up
        immediately as "pending"), then nudges both the UI refresh queue and
        the network thread.

        ``local_payload`` is what gets stored in ConversationLog.payload for
        local display; the network wire format comes from ``gcm`` itself.
        """
        send_op = SendOperation(
            # Must match convo.write_cap so find_resendable picks up our PWAL rows.
            bacap_stream=convo_state.own_peer_bacap_uuid,
            messages=[gcm],
        )
        print("serializing SendOperation for outgoing message", send_op)
        new_write_caps, db_entries = send_op.serialize(
            chunk_size=1530,  # TODO SphinxGeometry.somethingPayloadLength
            conversation_id=convo_state.conversation_id,
        )

        async with persistent.asession() as sess:
            for cap_uuid in new_write_caps:
                sess.add(persistent.WriteCapWAL(id=cap_uuid))
            for obj in db_entries:
                sess.add(obj)
            final_pwal_id = db_entries[-1].id  # relying on this being a plaintextwal is a little bit of an assumption about the internal of .serialize() ....

            # Then we pretend that we have received it:
            sess.add(persistent.ConversationLog(
                conversation_id=convo_state.conversation_id,
                conversation_peer_id=convo_state.own_peer_id,
                conversation_order=select(func.count())
                .select_from(persistent.ConversationLog)
                .where(persistent.ConversationLog.conversation_id == convo_state.conversation_id)
                .scalar_subquery(),
                payload=local_payload,
                network_status=1,
                outgoing_pwal=final_pwal_id,
            ))
            await sess.commit()

        # Wake the chat view (was missing from the old send_file path).
        await self.iothread.run_in_io(network.conversation_update_queue.put((convo_state.conversation_id, False)))

        # Signal the network module that we have a new outgoing message:
        await self.iothread.run_in_io(
            network.check_for_new()
        )

    @async_cb
    async def chat_msg_single_line(self):
        """Send a single line message to the currently selected chat window."""
        msg = self.ui.chat_lineEdit.text()
        self.ui.chat_lineEdit.setText("")
        convo_state = self.convo_state()
        if not convo_state:
            return
        convo_state.chat_lineEdit_buffer = ''
        if not msg.strip():
            return

        group_chat_message = GroupChatMessage(version=0,membership_hash=b"TODO"*(32//4),text=msg)

        await self._enqueue_outgoing_gcm(
            convo_state,
            group_chat_message,
            # TODO massive hack here because we don't reassemble sendops yet
            local_payload=b"F" + group_chat_message.to_cbor(),
        )

    async def receive_msg_listener(self):
        """Listen to the network thread to learn when it has updated a persistent.Conversation,
        and make the UI refresh with bells and whistles."""
        while True:
            (conversation_id, redraw_only) = await self.iothread.run_in_io(network.conversation_update_queue.get())
            while conversation_id not in self.conversation_state_by_id:
                logger.debug(f"conversation_id {conversation_id} not in conversation_state_by_id yet")
                await asyncio.sleep(1)  # encountered a race here once, where the UI hadn't loaded. not sure if still there.
            convo_state = self.conversation_state_by_id[conversation_id]
            if redraw_only:
                convo_state.conversation_log_model.redraw_network_status()
                continue
            convo_state.conversation_log_model.increment_row_count()
            # And then we can increment the row count to let the UI register it:

            # x) Scrolling - two cases:
            if convo_state is self.convo_state():
                #   x.1) Scrolling: Conversation is in focus:
                # TODO make which of these to do configurable:
                convo_state.chat_lines_scroll_idx = 1.0
                root = self.ui.qml_ChatLines.rootObject()
                await convo_state.update_first_unread(root.property("ctx").value("first_unread"))
                root.setProperty("ctx", convo_state.qml_ctx(root, settings=self.settings))
                #xx = self.ui.qml_ChatLines.rootObject().property("ctx")
                #print(xx)
                #import pdb;pdb.set_trace()
                #print("current", self.ui.ChatLines.verticalScrollBar().value())
                #print("next", min(
                #    1 + self.ui.ChatLines.verticalScrollBar().value(),
                #    conversation_order-3))
                #self.ui.ChatLines.scrollToBottom()
            else:
                #   x.2) Scrolling: Conversation is NOT in focus:
                print("NOT IN FOCUS")
                convo_state.chat_lines_scroll_idx += 1.0
                # convo_state.chat_lines_scroll_idx = conversation_order
                # TODO we should flash the contact entry somehow
                # TODO we should bump "unread message" counter

            # if the main window is not in focus, we should issue a notification:
            if not self.app.focusWidget():
                self.app.alert(self)
                # self.app.beep()
            self.systray.has_new_messages() # TODO move this into block above

    def convo_state(self) -> ConversationUIState:
        # TODO there must be a more graceful way to do this:
        idx = self.ui.contacts_treeWidget.selectionModel().currentIndex()
        if idx.parent().isValid():
            idx = idx.parent()
        idx = self.ui.contacts_treeWidget.model().mapToSource(idx)
        q = self.all_contacts.item(idx.row(), idx.column())
        #q = self.ui.contacts_treeWidget.currentItem()
        if not q:
            raise Exception("Can't find selected conversation")
        #q = q.parent() or q
        return self.conversation_state_by_id[q.conversation_id]

    @async_cb
    async def send_file(self):
        """Send each queued attachment as its own SendOperation.

        Wire format is a full GroupChatMessage (bytes in PWAL); the local
        ConversationLog row gets a lightweight ``file_outgoing`` marker so
        the chat view does not store megabytes inline.
        """
        convo = self.convo_state()
        print("should send files", convo.attached_files)

        voice_note_drafts = []
        audio = getattr(self, "_ptt_audio", None)
        # One SendOperation per file — unserialize() only decodes one GCM.
        for fn in sorted(convo.attached_files):
            f_path = Path(fn)
            is_draft = bool(audio and audio.is_draft_path(f_path))

            if not f_path.is_file():
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    f"Attachment no longer exists and was skipped:\n{f_path}",
                )
                continue

            size = f_path.stat().st_size
            # Same cap network._spill_attachment uses on receive.
            if size > network._ATTACHMENT_HARD_CAP:
                cap_mib = network._ATTACHMENT_HARD_CAP / (1024 * 1024)
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    f"{f_path.name} is {size / (1024 * 1024):.1f} MiB, which "
                    f"exceeds the {cap_mib:.0f} MiB attachment limit. "
                    "It was not sent.",
                )
                continue

            upload = GroupChatFileUpload.from_path(f_path)
            gcm = GroupChatMessage(
                version=0,
                membership_hash=b"TODO" * (32 // 4),  # TODO: convo_state.group_chat_state.membership_hash
                file_upload=upload,
            )

            # Store a lightweight local marker rather than the file bytes: the
            # renderer only needs the basename/filetype, and the bytes are
            # already on disk at src_path (empty for voice-note drafts, which
            # are discarded right after this send).
            local_payload = b"F" + cbor2.dumps({
                "v": 0,
                "kind": "file_outgoing",
                "basename": upload.basename,
                "filetype": upload.filetype,
                "size": size,
                "src_path": "" if is_draft else str(f_path),
            })

            await self._enqueue_outgoing_gcm(
                convo, gcm, local_payload=local_payload,
            )

            if is_draft:
                voice_note_drafts.append(f_path)

        # Remove sent files from model and view:
        convo.attached_files.clear()
        self.ui.attached_files_QListWidget.clear()
        self._update_attachment_controls()
        if audio:
            for draft_path in voice_note_drafts:
                audio.discard_draft(draft_path)

    @async_cb
    async def attach_file(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)  # allow more than one file
        dialog.setAcceptMode(QFileDialog.AcceptOpen)  # files should exist already
        dialog.setViewMode(QFileDialog.ViewMode.Detail)
        if saved := getattr(self, "saved_file_dialog", None):
            dialog.restoreState(saved)
        # setProxyModel() -- in case user enters a url?
        #.setHistory()
        if not dialog.exec():
            self.saved_file_dialog = dialog.saveState()
            return
        self.saved_file_dialog = dialog.saveState()
        print("file dialog", dialog.selectedFiles())
        convo = self.convo_state()

        # TODO may want to make the paths nicer, like relative to home folder or soomething like that
        convo.attached_files |= set(dialog.selectedFiles())  # add entries to convo_state
        self.refresh_attached_files_for_conversation(convo)

    def refresh_attached_files_for_conversation(self, convo: ConversationUIState):
        selected_path = self._selected_attachment_path()
        self.ui.attached_files_QListWidget.clear()
        audio = getattr(self, "_ptt_audio", None)
        selected_item = None
        for fname in convo.attached_files:
            itm = QListWidgetItem()
            itm.setData(0x100, fname)  # full path stored in Qt.UserRole
            p = Path(fname)
            abbrev : str = str(Path(p.parent.name) / p.name)
            try:
                bytesz = p.stat().st_size
                byteunit = "BKMGT"[int(math.log(1 + bytesz, 1024)):][:1] or "B"
                bytesz /= 1024**("BKMGT".index(byteunit))
                abbrev += f"\r\n({bytesz:,.2f}".rstrip("0").rstrip(".") + f"{byteunit})"
            except (FileNotFoundError, PermissionError):
                # TODO should report this error
                continue
            # Surface voice-note state directly in the attachment list so drafts
            # can be reviewed without adding another model or tab.
            if audio and audio.is_draft_path(p):
                abbrev = f"voice note draft\r\n{abbrev}"
            elif p.suffix.lower() == ".opus":
                abbrev = f"audio clip\r\n{abbrev}"
            itm.setText(abbrev)  # abbreviated path with first parent + size stored in "display"
            itm.setToolTip(str(p))
            self.ui.attached_files_QListWidget.addItem(itm)
            if selected_path == p:
                selected_item = itm

        has_files = self.ui.attached_files_QListWidget.count() > 0
        # Send is only meaningful when at least one file is queued.
        self.ui.send_file_button.setEnabled(has_files)

        if selected_item is not None:
            self.ui.attached_files_QListWidget.setCurrentItem(selected_item)
        elif has_files:
            self.ui.attached_files_QListWidget.setCurrentRow(0)
        self._update_attachment_controls()

    @async_cb
    async def conversation_selected(self, selected:QTreeWidgetItem, old:QTreeWidgetItem|None):
        # https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTreeWidget.html
        # scrollToItem ; setCurrentItem
        if not selected:
            return
        #import pdb;pdb.set_trace()
        print("selected", selected)
        if selected.parent().isValid():
            # peer name selected under a conversation, we want to look up the data for the conversation:
            selected = selected.parent()
        if old.parent().isValid():
            old = old.parent()
        selected_qmi = selected.model().mapToSource(selected)
        selected_id = self.all_contacts.item(selected_qmi.row(), selected_qmi.column()).conversation_id
        selected = selected.data()  # type: ignore[call-arg]
        # clicks may be on either convo or contact:
        conversation = self.conversation_state_by_id[selected_id]
        #old = getattr(old, "parent", lambda: None)() or old
        old_convo = None
        if old and old.model():
            old = old.model().mapToSource(old)
            old_id = self.all_contacts.item(old.row(), 0).conversation_id
            old_convo = self.conversation_state_by_id[old_id]
        print("conversation selected", selected)
        ### TODO this was how far we got

        if old_convo:
            # Store old line edit buffer and scroll
            old_convo.chat_lineEdit_buffer = self.ui.chat_lineEdit.text()
            if old_ctx := self.ui.qml_ChatLines.rootObject().property("ctx"):
                print("old first_unread is", old_ctx.value("first_unread"))
                await old_convo.update_first_unread(old_ctx.value("first_unread"))
            root = self.ui.qml_ChatLines.rootObject()
            if root and (vscrollbar := root.findChild(object, "vscrollbar")):
                #vrect = vscrollbar.findChild(object, "vscrollbar_rect")
                #vh = root.height()
                #vcih = c.property("contentHeight")
                #vpos = (vh - vcih) / vh / 2
                #print("so pos", vpos)
                # vscrollbar.position() is NOT the same as vscrollbar.property("position")
                #print("vscrollbar.property.position was", vscrollbar.property("position"))
                #off = vscrollbar.findChild(object,"vscrollbar_rect").height() / vscrollbar.height()
                #print('calculated off', off)
                #off *= 2
                #off = 0
                #print("adjusted:", vscrollbar.property("position")  + off, off)
                #old_convo.chat_lines_scroll_idx = vscrollbar.property("position") #+ off
                #import pdb;pdb.set_trace()
                #print("saved scroll", old_convo.chat_lines_scroll_idx, off)
                pass
        convo_state = self.convo_state()
        if not convo_state:
            return
        # When user selects a conversation we need to replace the chat model with:
        #self.ui.quickWidget.setSource("http://127.0.0.1:2222")
        # Feed "initial properties" into QML; not sure how to force a reload except
        # setSource() again, but presumably QML can read updated properties set with
        #if c := self.ui.qml_ChatLines.rootObject()
        #c.dumpItemTree()
        #c.property("conversation_name")
        #import pdb;pdb.set_trace()
        print("load root", self.ui.qml_ChatLines.rootObject())
        if root := self.ui.qml_ChatLines.rootObject():
            print("rootObject is", root, root.property("ctx"), self.ui.qml_ChatLines.property("ctx"))
            rctx = self.ui.qml_ChatLines.rootContext()
            print("root context", self.ui.qml_ChatLines.rootContext())
            # >>> rctx.setContextProperties([PySide6.QtQml.QQmlContext.PropertyPair(name="ctx", value=1)])

            #import pdb;pdb.set_trace()
            props = convo_state.qml_ctx(root, settings=self.settings)
            root.setProperty("ctx", props)
            #convo_state = self.convo_state()
            # backend.set_convo_model_and_scroll()
            vscrollbar = root.findChild(object, "vscrollbar")
            #vscrollbar.setProperty("position", convo_state.chat_lines_scroll_idx)
            #print('set first restored scroll to', convo_state.chat_lines_scroll_idx, vscrollbar.property("position"))
        else:
            props = convo_state.qml_ctx(None, settings=self.settings)
            print("root context", self.ui.qml_ChatLines.rootContext())
            self.ui.qml_ChatLines.setInitialProperties({
                "ctx": props,
            })
            self.ui.qml_ChatLines.setSource("resources/chatview.qml")
            self.app.processEvents()  # wait for .rootObject() to be created
            root = self.ui.qml_ChatLines.rootObject()
            root.setProperty("ctx", convo_state.qml_ctx(root, settings=self.settings))
            print("init first_unread", self.ui.qml_ChatLines.rootObject().property("ctx").value("first_unread"))

            #self.ui.qml_ChatLines.rootObject().setProperty("ctx", props)
            # >>> ct=PySide6.QtQml.QQmlContext(self.ui.qml_ChatLines.rootContext(), objParent=self.ui.qml_ChatLines.rootObject())
            #import pdb;pdb.set_trace()
            #self.ui.qml_ChatLines.rootContext().setContextObject(self.ui.qml_ChatLines.rootObject())
            #self.ui.qml_ChatLines.setProperty("ctx", props)
            #self.ui.qml_ChatLines.rootContext().setProperty("ctx", props)
            #self.ui.qml_ChatLines.rootContext().setProperty("backend", backend)
            #self.ui.qml_ChatLines.rootContext().setContextProperty("backend2", backend)
            #backend.set_convo_model_and_scroll()

        # Restore new single line input buffer
        self.ui.chat_lineEdit.setText(convo_state.chat_lineEdit_buffer)
        # Update the label we display at the top left corner of the chat view:
        self.ui.ContactName.setText(f"{selected} (your name: {convo_state.own_peer_name})")

        # The whole attach-file tab and its key children start disabled in the
        # generated UI; enable them once a real conversation is in scope.
        self.ui.attach_file_tab.setEnabled(True)
        self.ui.attach_file_button.setEnabled(True)
        self.ui.attached_files_QListWidget.setEnabled(True)

        # Restore attached_files:
        self.refresh_attached_files_for_conversation(convo_state)

        self.systray.has_read_messages()

    def do_we_even_have_unread_messages(self) -> bool:
        """
        We need to keep track of "read" per conversation.
        This needs to be persisted to disk.
        Then we need to keep an index of conversations that have unread messages,
        with counts, so we can display them in the contact roster,
        and we should be able to produce the sum so we can display that somewhere too.
        The sum would then also be used to govern the systray icon.
        """
        return False

    @async_cb
    async def new_conversation(self):
        conversation_name , ok = QInputDialog.getText(
            self,
            "New conversation",
            "Choose conversation title:",
        )
        if not ok: return
        # TODO this crap out while something is awaiting the write_channel:
        display_name , ok = QInputDialog.getText(
            self,
            "New conversation",
            "Choose (your) name displayed to the other user(s):",
        )
        if not ok: return

        wcapwal = persistent.WriteCapWAL(id=uuid.uuid4()) # actual CAP will be provisioned later
        rcapwal = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcapwal.id)

        convo = persistent.Conversation(name=conversation_name, write_cap=wcapwal.id, first_unread=0)

        own_peer = persistent.ConversationPeer(
            name=display_name, read_cap_id=rcapwal.id,
            active=False, # we are not reading from ourself
            conversation=convo)
        convo.own_peer = own_peer
        # TODO this is mainly for debugging, but our view requires at least one message or we can't send new msgs:
        first_post = persistent.ConversationLog(
            conversation=convo,
            conversation_peer=own_peer,
            conversation_order=0,
            payload=b"Your name in this conversation is " + own_peer.name.encode(),
        )
        async with persistent.asession() as sess:
            sess.add(wcapwal)
            sess.add(rcapwal)
            sess.add(convo)
            sess.add(own_peer)
            sess.add(first_post)
            await sess.commit()
            await sess.refresh(first_post)
            await sess.refresh(own_peer)
            await sess.refresh(convo)
            await sess.refresh(rcapwal)
            await sess.refresh(wcapwal)
        await add_conversation(self, convo)

    @async_cb
    async def generate_voucher(self):
        """Joiner side of the Contact Voucher protocol: mint a Voucher over the
        selected conversation's MessageStream, publish it, and show the token to
        hand to an existing member out of band. The join completes in the
        background once that member replies over the rendezvous stream."""
        try:
            convo = self.convo_state()
        except Exception:
            QMessageBox.critical(
                self, f"ERROR: {APP_NAME}",
                "Select a conversation first (or create one) before generating a voucher.",
            )
            return

        conv_id = convo.conversation_id
        # You can only join a conversation once. If the client is already a
        # member, a second voucher would re-run the join and duplicate every
        # member and message, so refuse outright.
        if await self.iothread.run_in_io(conversation_is_joined(conv_id)):
            QMessageBox.information(
                self, APP_NAME,
                "You are already a member of this conversation, so a voucher is "
                "not needed. Vouchers are only for joining a conversation you are "
                "not yet part of.",
            )
            return
        # At most one voucher in flight per conversation. If one is pending,
        # offer to abandon it and mint a fresh one (e.g. the code was lost).
        pending_id = await self.iothread.run_in_io(pending_voucher_for(conv_id))
        if pending_id is not None:
            if QMessageBox.question(
                self, APP_NAME,
                "A voucher for this conversation is already pending. Cancel it "
                "and generate a new one?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                defaultButton=QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            await self.iothread.run_in_io(cancel_pending_voucher(pending_id))

        display_name, ok = QInputDialog.getText(
            self, "Generate voucher",
            "Choose (your) name shown to the contact who inducts you:",
        )
        if not ok or not display_name:
            return

        try:
            voucher = await self.iothread.run_in_io(
                mint_and_publish(self.iothread.kp_client, conv_id, display_name)
            )
        except Exception as e:
            QMessageBox.critical(
                self, f"ERROR: {APP_NAME}",
                "Could not mint the voucher. Is the conversation finished setting "
                f"up and the client connected?\n\n{e}",
            )
            return

        code = b64encode(voucher).decode()
        QTimer.singleShot(0, lambda: QMessageBox.information(
            self, f"Voucher: {APP_NAME}",
            f"Here is your voucher, {display_name}.\nHand it out of band to an "
            f"existing member, who will induct you:\n\n{code}",
        ))
        # Completion is asynchronous: poll for the inductor's reply, then move
        # this conversation onto the salt-mutated stream and add the members it
        # names. PendingVoucher persists the handshake, so a restart resumes it.
        ensure_future(self._await_voucher_join(convo))

    async def _await_voucher_join(self, convo):
        try:
            added = await self.iothread.run_in_io(
                await_and_open(self.iothread.kp_client, convo.conversation_id)
            )
        except Exception as e:
            logging.warning("voucher await failed: %s", e)
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, f"ERROR: {APP_NAME}", f"The voucher join did not complete:\n{e}",
            ))
            return
        for name in added:
            convo.contacts_standard_item.appendRow(QStandardItem(name))
        await self.iothread.run_in_io(network.signal_readables_to_mixwal())
        joined = ", ".join(added) or "(none)"
        QTimer.singleShot(0, lambda: QMessageBox.information(
            self, f"Joined: {APP_NAME}", f"You have joined. Members added: {joined}.",
        ))

    @async_cb
    async def induct_via_voucher(self):
        """Inductor side of the Contact Voucher protocol: read the joiner's
        published voucher, reply over the rendezvous stream with this group's
        read caps, and add the joiner on their salt-mutated read cap."""
        try:
            convo = self.convo_state()
        except Exception:
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, "ERROR",
                "Create or select a conversation before inducting a contact.",
            ))
            return

        voucher_str, ok = QInputDialog.getText(
            self, "Induct via voucher",
            "Enter the voucher your new contact handed you:",
        )
        if not ok or not voucher_str:
            return
        try:
            voucher = b64decode(voucher_str.strip().encode())
        except Exception:
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, "ERROR", "Invalid voucher code.",
            ))
            return

        try:
            joiner_name = await self.iothread.run_in_io(
                derive_read_and_induct(
                    self.iothread.kp_client, convo.conversation_id, "", voucher,
                )
            )
        except Exception as e:
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, f"ERROR: {APP_NAME}", f"Induction failed:\n{e}",
            ))
            return

        name = joiner_name or "contact"
        convo.contacts_standard_item.appendRow(QStandardItem(name))
        logging.warning("Peer inducted. Signaling readables_to_mixwal")
        await self.iothread.run_in_io(network.signal_readables_to_mixwal())
        QTimer.singleShot(0, lambda: QMessageBox.information(
            self, f"Inducted: {APP_NAME}", f"Inducted {name} into this conversation.",
        ))

    @async_cb
    async def show_pending_vouchers(self):
        """Open the pending-voucher view so the user can abandon stale ones."""
        rows = await self.iothread.run_in_io(list_pending_vouchers())
        if not rows:
            QMessageBox.information(self, APP_NAME, "There are no pending vouchers.")
            return
        dialog = PendingVouchersDialog(self, rows)
        dialog.exec()
        for pv_id in dialog.cancelled:
            await self.iothread.run_in_io(cancel_pending_voucher(pv_id))

    def close(self, *args, **kwargs):
        if kwargs.get('really_quit', False):
            logger.critical("QUITTING")
            network.shutdown()
            self.systray = False
            self.app.quit()
    def closeEvent(self, event, **kwargs):
        if getattr(self, "systray", False):
            event.ignore()
            self.systray.showMessage('Still running', f"{APP_NAME} running in background.")
            self.hide()

class Backend(QObject):
    updated = Signal(str, arguments=[])
    def set_convo_model_and_scroll(self):
        self.updated.emit("a")

class MixSystrayIcon(QSystemTrayIcon):
    """https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html

    TODO: when user clicks on the systray icon we should scroll to an unread message?
    TODO: to do that we'd have to keep track of what they have seen.
    """
    def __init__(self, mw : MainWindow, echomix_icon: QIcon):
        super().__init__()
        if not self.isSystemTrayAvailable():
            return
        mw.systray : "MixSystrayIcon" = self
        self.mw : MainWindow = mw

        #for icon_name in ( "SP_DialogYesButton",
        #                   "SP_MessageBoxCritical",
        #                   "SP_MessageBoxInformation",
        #                  ):
        #    pixmapi = getattr(QStyle, icon_name)
        #    icon : QIcon = mw.style().standardIcon(pixmapi)
        #    setattr(self, icon_name, icon)
        #self.setIcon(self.SP_MessageBoxInformation)  # new message
        #self.setIcon(self.SP_DialogYesButton)  # connected
        #self.setIcon(self.SP_MessageBoxCritical)  # disconnected
        self.setIcon(echomix_icon)
        self.setToolTip(APP_NAME)
        self.activated.connect(self.on_left_click)

        # Context menu for right-clicking on the systray icon:
        self.setContextMenu(QMenu())
        # TODO: Enable/Disable notifications
        self.contextMenu().addAction(mw.ui.action_quit)

        self.show()
        if self.supportsMessages():
            pass #self.showMessage("hel","lo")
        # there's also this for bugging the user:
        # https://doc.qt.io/qt-6/qapplication.html#alert

    def has_new_messages(self) -> None:
        logger.warning("has_new_messages")
        self.setIcon(self.mw.echomix_icon_new_message)
        self.mw.setWindowIcon(self.mw.echomix_icon_new_message)

    def has_read_messages(self) -> None:
        logger.warning("has_read_messages")
        self.setIcon(self.mw.echomix_icon)
        self.mw.setWindowIcon(self.mw.echomix_icon)

    def showMessage(self, title:str, message:str) -> None:  # type: ignore[override]
        # icon:QIcon|QPixmap, /, msTimeoutHint:int|None=None
        super().showMessage(title, message)
        #if icon is not None:
        #    if msTimeoutHint is not None:
        #        super().showMessage(title, message, icon, msTimeoutHint)
        #    else:
        #        super().showMessage(title, message, icon)
        #else:
        #    super().showMessage(title, message)

    def on_left_click(self, reason: QSystemTrayIcon.ActivationReason):
        self.mw.show()  # if they closed the window
        self.mw.raise_()  # raise to top  /  make active window in WM

        # TODO we need to ensure
        self.has_read_messages()
        # has a chance to run here, but it should only do that when we actually resume
        # to the conversation that has the new messages.

    def messageClicked(self,*args,**kwargs):
        print("Someone clicked message", args, kwargs)

    
async def add_conversation(window, convo: persistent.Conversation) -> None:
    window.conversation_log_models = getattr(window, "conversation_log_models", dict())

    contacts = window.ui.contacts_treeWidget
    #qtwi = QTreeWidgetItem([convo.name])
    qtwi = QStandardItem(convo.name)
    qtwi.conversation_id = convo.id
    clm = ConversationLogModel(convo.id)
    convo_state = ConversationUIState(
        own_peer_id=convo.own_peer_id,
        own_peer_name=convo.own_peer.name,
        own_peer_bacap_uuid=convo.write_cap,
        conversation_id=convo.id,
        contacts_standard_item=qtwi,
        chat_lineEdit_buffer="",
        conversation_log_model=clm,
        chat_lines_scroll_idx=1.0,
        first_unread=convo.first_unread or 0,
    )
    window.conversation_state_by_id[convo.id] = convo_state

    for peer in convo.peers:
        #ptwi = QTreeWidgetItem([peer.name])
        ptwi = QStandardItem(peer.name)
        qtwi.setChild(qtwi.rowCount(), ptwi)  # can we use qtwi.appendRow(ptwi) here?

    async with persistent.asession() as sess:
        msg_count = (await sess.exec(
            select(func.count())
            .select_from(persistent.ConversationLog)
            .where(persistent.ConversationLog.conversation_id == convo.id)
        )).first()
        convo_state.conversation_log_model.row_count = msg_count
    convo_state.chat_lines_scroll_idx = 1.0  # initially we scroll to bottom

    # Append the new conversation to the "real" model window.all_contacts,
    # then figure out where that sits in the FilterProxyModel used for sorting contacts,
    # and make the new convo selected:
    window.all_contacts.appendRow(qtwi)  # append conversation to model
    real_index = window.all_contacts.index(window.all_contacts.rowCount()-1, 0)
    pwmi = window.ui.contacts_treeWidget.model().mapFromSource(real_index)
    if pwmi.isValid():
        # Select the new conversation, if the filter would display it.
        # We could maybe clear the existing filter in case it doesn't.
        window.ui.contacts_treeWidget.setCurrentIndex(pwmi)
        window.ui.contacts_treeWidget.expand(pwmi)

    # ULTRA TODO THIS IS FOR DEBUGGING ONLY
    #qts = QAbstractItemModelTester(clm)
    #print(qts)
    for _ in range(3): # yield a bunch to make Qt not complain ...
        await asyncio.sleep(0.001)

def rebuild_pydantic_models():
    """Updated pydantic models with the classes defined in this file"""
    #ConversationUIState.model_rebuild()
    pass

async def main(window: MainWindow):
    def report_exception2(*args):
        for m in args:
            print("report_exception2", m)
            QTimer.singleShot(0, lambda: QMessageBox.critical(window, f"Exception", f"{m}"))
    asyncio.get_running_loop().set_exception_handler(report_exception2)

    rebuild_pydantic_models()
    echomix_icon = QIcon()
    window.echomix_icon = echomix_icon
    for sz in (16,32,48,160,256):
        echomix_icon.addFile(f"resources/echomix_{sz}.png", size=QSize(sz,sz))
        # addFile(..., mode=QIcon.Mode.(Normal|Disabled|Active|Selected), state=QIcon.State.Off|QIcon.State.Off)
        # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QIcon.html#PySide6.QtGui.QIcon.Mode
        # https://doc.qt.io/qtforpython-6/PySide6/QtGui/QIcon.html#PySide6.QtGui.QIcon.State
        # offline: Disabled
    window.echomix_icon_new_message=QIcon()
    window.echomix_icon_new_message.addFile("resources/echomix_256_new_message.png", QSize(256,256))
    echomix_icon.addFile("resources/echomix_icon.svg")

    systray = MixSystrayIcon(window, echomix_icon)

    #contacts.focusInEvent = lambda x: print('in', x)
    #contacts.focusOutEvent = lambda x: print('out', x)

    window.setWindowIcon(echomix_icon)
    # window.findChild(object, "mdstatus").setText("# HEI")
    #window.ui.mdstatus.setText("# HEI")
    contacts : QTreeView = window.ui.contacts_treeWidget

    # populate setting and contacts from persistent.py:
    window.settings: dict[str, str|int|None] = dict()
    with persistent.Session(persistent._engine_sync) as sess:
        settings = sess.exec(select(persistent.AppSetting))
        for setting in settings:
            parsed = None
            if setting.type == 'str':
                parsed = setting.value
            elif setting.type == 'int':
                parsed = int(setting.value)
            window.settings[setting.id] = parsed
        a = sess.exec(
            select(persistent.Conversation)
        ) # TODO sometimes this doesn't work when the network asyncio is also just getting started.
        for convo in a:
            await add_conversation(window, convo)

    window.show()
    create_task(window.receive_msg_listener())

def todo_settings():
    # https://doc.qt.io/qtforpython-6/examples/example_corelib_settingseditor.html
    #org_settings = QSettings(QSettings.IniFormat, APP_ORGANIZATION)  # for other Qt apps by same APP_ORGANIZATION
    #app_settings = QSettings(QSettings.IniFormat, APP_ORGANIZATION, APP_NAME)
    # .status() https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html#PySide6.QtCore.QSettings.Status
    # $HOME/.config/MySoft/Star Runner.conf
    # $HOME/.config/MySoft.conf

    # https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html
    pass

# Make the fd global so it doesn't go out of scope when we return
# since close(2) will unlock:
already_running_fd: "typing.IO | None" = None

def is_there_already_an_instance_running() -> bool:
    """
    Checks if there are other instances of the application running.

    Returns
      False: There is already an app instance running
      True: This is the first app instance
    """
    # TODO: use a config file entry or something else than __FILE__:
    global already_running_fd
    already_running_fd = open(persistent.state_file, "rb")
    try:
        fcntl.flock(already_running_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True  # Already running
    return False  # Obtained the lock

def error_and_exit(app: QApplication, why: str, main_window: QMainWindow | None=None) -> None:
    if not main_window:
        main_window = QMainWindow()
    dlg = QMessageBox.critical(main_window, f"ERROR: {APP_NAME}", why)
    app.quit()
    sys.exit(why)

def get_all_loggers():
    return set(logging.root.manager.loggerDict.keys())

def add_log_args(parser: argparse.ArgumentParser):
    for ln in get_all_loggers():
        fmt = logging.Formatter("%(name)s - %(levelname)s")
        lnlog = logging.getLogger(ln)
        if lnlog.hasHandlers():
            lnlog.handlers.clear()
        ch = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s %(name)s: %(levelname)s: %(message)s")
        ch.setFormatter(fmt)
        lnlog.addHandler(ch)
        print("log fmt set", ln)
        lnlog.critical("test")
    parser.add_argument(
        '--level',
        type=str, nargs=2,
        metavar=("LOGGER", "LEVEL"),
        help=f"Override log level for LOGGER. Available loggers: {', '.join(get_all_loggers())}",
        action="append",
    )

def cli():
    # The chat view is a QQuickWidget. Qt Quick's default GL/RHI backend
    # renders it solid black on displays without working hardware GL (SSH
    # X-forwarding, VNC, headless or software-GL remotes), which is how this
    # client is often run. Default to the software renderer, which composites
    # reliably everywhere; set QT_QUICK_BACKEND yourself to override. Must be
    # set before the QApplication is constructed.
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    app = QApplication(sys.argv)
    parser = argparse.ArgumentParser()
    add_log_args(parser)
    args = parser.parse_args()
    app.setStyle("Fusion")

    logger.critical("going to init_and_migrate")
    try:
        persistent.init_and_migrate()  # this must be run outside the async loop
    except Exception as e:
        error_and_exit(app, f"Database schema migration failed:\n{repr(e)}")

    logging.getLogger('sqlalchemy.engine.Engine').disabled=True
    if args.level:
        for logger_name, level in args.level:
            log_level = getattr(logging, level.upper(), None)
            if log_level:
                logging.getLogger(logger_name).disabled = False
                logging.getLogger(logger_name).setLevel(log_level)
                logging.getLogger(logger_name).critical(f"set to {level.upper()}({log_level})")
            else:
                logging.getLogger(logger_name).disabled = True

    logger.critical("checking for instance")
    if is_there_already_an_instance_running():
        error_and_exit(app, f"{APP_NAME} is already running.")

    # Make scrolling suck less, https://bugreports.qt.io/browse/QTBUG-123936
    os.environ["QT_QUICK_FLICKABLE_WHEEL_DECELERATION"] = "14999"

    iothread = AsyncioThread()
    iothread.start()

    logger.critical("asyncio started")
    window = MainWindow(app)
    window.iothread = iothread

    return QtAsyncio.run(main(window), handle_sigint=True)
