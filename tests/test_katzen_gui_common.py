from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import ClassVar, NamedTuple

import pytest
import pytest_asyncio

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QMessageBox,
    QWidget,
)

from katzenqt import katzen, network, persistent  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000200000002080600000072b6"
    "0d240000001149444154789c63f8cfc0f01f8419600c0047ca07f967596eb7"
    "0000000049454e44ae426082"
)


class RecordedBox(NamedTuple):
    kind: str
    title: str
    text: str


class FakeMessageBox:

    Icon = QMessageBox.Icon
    StandardButton = QMessageBox.StandardButton

    seen: ClassVar[list[RecordedBox]] = []
    answer: ClassVar[QMessageBox.StandardButton] = QMessageBox.StandardButton.No

    def __init__(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        parent: QWidget | None = None,
    ) -> None:
        self.icon = icon
        self.title = title
        self.text = text
        self.parent_widget = parent
        self.text_format: object = None
        self.standard_buttons: object = None
        self.default_button: object = None

    def setTextFormat(self, fmt: object) -> None:
        self.text_format = fmt

    def setStandardButtons(self, buttons: object) -> None:
        self.standard_buttons = buttons

    def setDefaultButton(self, button: object) -> None:
        self.default_button = button

    def exec(self) -> QMessageBox.StandardButton:
        type(self).seen.append(RecordedBox("exec", self.title, self.text))
        return type(self).answer

    @classmethod
    def _record(cls, kind: str, title: str, text: str) -> (
        QMessageBox.StandardButton
    ):
        cls.seen.append(RecordedBox(kind, title, text))
        return cls.answer

    @classmethod
    def warning(
        cls, parent: QWidget | None, title: str, text: str,
    ) -> QMessageBox.StandardButton:
        return cls._record("warning", title, text)

    @classmethod
    def critical(
        cls, parent: QWidget | None, title: str, text: str,
    ) -> QMessageBox.StandardButton:
        return cls._record("critical", title, text)

    @classmethod
    def information(
        cls, parent: QWidget | None, title: str, text: str,
    ) -> QMessageBox.StandardButton:
        return cls._record("information", title, text)

    @classmethod
    def texts(cls) -> list[str]:
        return [box.text for box in cls.seen]


class FakeFileDialog:

    FileMode = QFileDialog.FileMode
    AcceptMode = QFileDialog.AcceptMode
    AcceptOpen = QFileDialog.AcceptMode.AcceptOpen
    ViewMode = QFileDialog.ViewMode

    accepted: ClassVar[bool] = True
    files: ClassVar[list[str]] = []
    restored: ClassVar[list[bytes]] = []

    def __init__(self) -> None:
        self.file_mode: object = None
        self.accept_mode: object = None
        self.view_mode: object = None

    def setFileMode(self, mode: object) -> None:
        self.file_mode = mode

    def setAcceptMode(self, mode: object) -> None:
        self.accept_mode = mode

    def setViewMode(self, mode: object) -> None:
        self.view_mode = mode

    def restoreState(self, state: bytes) -> bool:
        type(self).restored.append(state)
        return True

    def saveState(self) -> bytes:
        return b"dialog-state"

    def exec(self) -> int:
        return 1 if type(self).accepted else 0

    def selectedFiles(self) -> list[str]:
        return list(type(self).files)


def _reset_doubles() -> None:
    FakeMessageBox.seen = []
    FakeMessageBox.answer = QMessageBox.StandardButton.No
    FakeFileDialog.accepted = True
    FakeFileDialog.files = []
    FakeFileDialog.restored = []


class VoiceDraft(NamedTuple):
    path: Path
    duration_seconds: float
    file_size_bytes: int


class FakeAudio:

    def __init__(self, root: Path) -> None:
        self.root = root
        self.drafts_dir = root / "drafts"
        self.received_dir = root / "received"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.received_dir.mkdir(parents=True, exist_ok=True)
        self.is_playing = False
        self.playback_error: str | None = None
        self.started: list[int] = []
        self.previewed: list[Path] = []
        self.played: list[Path] = []
        self.discarded: list[Path] = []
        self.stops = 0
        self.cancels = 0
        self.raise_on: set[str] = set()
        self.active_draft_path: Path | None = None

    def _maybe_raise(self, name: str) -> None:
        if name in self.raise_on:
            raise katzen.AudioEngineError(f"{name} failed")

    def start_capture(self, conversation_id: int) -> Path:
        self._maybe_raise("start_capture")
        self.started.append(conversation_id)
        path = self.drafts_dir / f"conversation-{conversation_id}-draft.opus"
        path.write_bytes(b"opusdraft")
        self.active_draft_path = path
        return path

    def stop_capture(self) -> VoiceDraft:
        self._maybe_raise("stop_capture")
        path = self.active_draft_path or (self.drafts_dir / "draft.opus")
        path.write_bytes(b"opusdraft")
        return VoiceDraft(path, 1.5, path.stat().st_size)

    def cancel_capture(self) -> bool:
        self._maybe_raise("cancel_capture")
        self.cancels += 1
        return True

    def play_preview(self, path: Path) -> None:
        self._maybe_raise("play_preview")
        self.previewed.append(Path(path))
        self.is_playing = True

    def play_received(self, path: Path) -> None:
        self._maybe_raise("play_received")
        self.played.append(Path(path))
        self.is_playing = True

    def stop_playback(self) -> None:
        self._maybe_raise("stop_playback")
        self.stops += 1
        self.is_playing = False

    def take_playback_error(self) -> str | None:
        self._maybe_raise("take_playback_error")
        error, self.playback_error = self.playback_error, None
        return error

    def cache_received_clip(
        self, message_id: str, basename: str, payload: bytes,
    ) -> Path:
        path = self.received_dir / f"{message_id}-{basename}"
        path.write_bytes(payload)
        return path

    def is_draft_path(self, path: Path) -> bool:
        return Path(path).parent == self.drafts_dir

    def discard_draft(self, path: Path) -> None:
        self.discarded.append(Path(path))
        Path(path).unlink(missing_ok=True)


class FakeIoThread:

    def __init__(self) -> None:
        self.kp_client = object()
        self.engine_warmed = threading.Event()
        self.engine_warmed.set()
        self.alive = True
        self.ran = 0

    def is_alive(self) -> bool:
        return self.alive

    async def run_in_io(
        self, coroutine: Coroutine[object, object, object],
    ) -> object:
        self.ran += 1
        return await coroutine


class FakeSystray:
    def __init__(self) -> None:
        self.new_messages = 0
        self.read_messages = 0
        self.messages: list[tuple[str, str]] = []

    def has_new_messages(self) -> None:
        self.new_messages += 1

    def has_read_messages(self) -> None:
        self.read_messages += 1

    def showMessage(self, title: str, message: str) -> None:
        self.messages.append((title, message))


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> Iterator[QApplication]:
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    yield app


@pytest.fixture()
def window(
    qt_app: QApplication,
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[katzen.MainWindow]:
    monkeypatch.chdir(REPO_ROOT)
    palette = QPalette(qt_app.palette())
    main_window = katzen.MainWindow(qt_app)
    main_window.settings = {}
    main_window.iothread = FakeIoThread()
    main_window.systray = FakeSystray()
    yield main_window
    main_window.push_to_talk_watchdog.stop()
    main_window._playback_error_timer.stop()
    main_window.systray = None
    main_window.hide()
    qt_app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 250)
    main_window.deleteLater()
    qt_app.sendPostedEvents(main_window, QEvent.Type.DeferredDelete)
    qt_app.setPalette(palette)


@pytest.fixture()
def fresh_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "conversation_update_queue",
        "peer_added_queue",
        "substream_progress_queue",
        "tally_update_queue",
    ):
        monkeypatch.setattr(network, name, asyncio.Queue())


@pytest.fixture()
def audio(
    window: katzen.MainWindow, tmp_path: Path,
) -> FakeAudio:
    bridge = FakeAudio(tmp_path / "audio")
    window._ptt_audio = bridge
    return bridge


@pytest.fixture()
def boxes(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeMessageBox]]:
    _reset_doubles()
    monkeypatch.setattr(katzen, "QMessageBox", FakeMessageBox)
    yield FakeMessageBox
    _reset_doubles()


@pytest.fixture()
def instant_timer(
    boxes: type[FakeMessageBox], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fire(msec: int, callback: Callable[[], None]) -> None:
        callback()

    monkeypatch.setattr(katzen.QTimer, "singleShot", staticmethod(fire))


class SeededConversation(NamedTuple):
    conversation_id: int
    own_peer_id: int
    write_cap: uuid.UUID
    read_cap: uuid.UUID


async def seed_conversation(
    name: str = "testroom", own_name: str = "me", peers: tuple[str, ...] = (),
) -> SeededConversation:
    wcapwal = persistent.WriteCapWAL(id=uuid.uuid4())
    rcapwal = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcapwal.id)
    convo = persistent.Conversation(
        name=name, write_cap=wcapwal.id, first_unread=0,
    )
    own_peer = persistent.ConversationPeer(
        name=own_name, read_cap_id=rcapwal.id, active=False, conversation=convo,
    )
    convo.own_peer = own_peer
    first_post = persistent.ConversationLog(
        conversation=convo, conversation_peer=own_peer, conversation_order=0,
        payload=b"opening line",
    )
    extra: list[object] = []
    for peer_name in peers:
        peer_rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=b"r" * 136)
        extra.append(peer_rcap)
        extra.append(persistent.ConversationPeer(
            name=peer_name, read_cap_id=peer_rcap.id, active=True,
            conversation=convo,
        ))
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(first_post)
        for row in extra:
            sess.add(row)
        await sess.commit()
        await sess.refresh(convo)
        await sess.refresh(own_peer)
        await sess.refresh(wcapwal)
        await sess.refresh(rcapwal)
        return SeededConversation(
            convo.id, own_peer.id, wcapwal.id, rcapwal.id,
        )


async def add_seeded_conversation(
    win: katzen.MainWindow, conversation_id: int,
) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        convo = sess.get(persistent.Conversation, conversation_id)
        assert convo is not None
        await katzen.add_conversation(win, convo)


@pytest_asyncio.fixture
async def loaded_window(
    window: katzen.MainWindow,
) -> katzen.MainWindow:
    seeded = await seed_conversation()
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    return window


async def drain_tasks() -> None:
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass
