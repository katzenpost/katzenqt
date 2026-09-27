from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, QPoint  # noqa: E402
from PySide6.QtGui import QStandardItem  # noqa: E402
from PySide6.QtQuickWidgets import QQuickWidget  # noqa: E402
from PySide6.QtWidgets import QMenu, QTabWidget  # noqa: E402

from katzenqt import katzen, network  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    FakeMessageBox,
    add_seeded_conversation,
    boxes,
    fresh_queues,
    loaded_window,
    proxy_of,
    qt_app,
    seed_conversation,
    systray_of,
    window,
)
from tests.stubs import appending, ignore


class IoThreadStartup:
    def __init__(
        self,
        order: list[str],
        report: Callable[[object, dict[str, object]], None],
    ) -> None:
        self.order = order
        self.report = report


@pytest.fixture()
def io_thread_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[IoThreadStartup]:
    thread = katzen.AsyncioThread()
    order: list[str] = []

    async def warm() -> None:
        order.append("warm_engine")

    async def reconcile() -> None:
        order.append("reconcile_tally_once")

    async def async_main() -> None:
        order.append("async_main")
        thread.kp_client = cast(Any, "the client")

    async def background(client: object) -> None:
        order.append(f"start_background_threads:{client}")

    monkeypatch.setattr(thread, "warm_engine", warm)
    monkeypatch.setattr(thread, "reconcile_tally_once", reconcile)
    monkeypatch.setattr(thread, "async_main", async_main)
    monkeypatch.setattr(network, "start_background_threads", background)
    thread.run()
    handler = thread.loop.get_exception_handler()
    assert handler is not None
    try:
        yield IoThreadStartup(order, cast(Any, handler))
    finally:
        thread.loop.close()


def test_the_io_thread_runs_every_startup_step_in_order(
    io_thread_startup: IoThreadStartup,
) -> None:
    assert io_thread_startup.order == [
        "warm_engine",
        "reconcile_tally_once",
        "async_main",
        "start_background_threads:the client",
    ]


def test_a_cancelled_io_task_is_not_reported(
    io_thread_startup: IoThreadStartup,
    capsys: pytest.CaptureFixture[str],
) -> None:
    io_thread_startup.report(None, {"exception": asyncio.CancelledError()})
    assert capsys.readouterr().out == ""


def test_a_failed_io_task_is_reported(
    io_thread_startup: IoThreadStartup,
    capsys: pytest.CaptureFixture[str],
) -> None:
    io_thread_startup.report(None, {"exception": RuntimeError("boom")})
    assert "AsyncioThread exception" in capsys.readouterr().out


def test_an_io_loop_context_without_an_exception_is_reported(
    io_thread_startup: IoThreadStartup,
    capsys: pytest.CaptureFixture[str],
) -> None:
    io_thread_startup.report(None, {"message": "handle is closed"})
    assert "AsyncioThread exception" in capsys.readouterr().out


class JumpingClock:
    def __init__(self) -> None:
        self.readings = 0

    def monotonic(self) -> float:
        self.readings += 1
        return 0.0 if self.readings == 1 else 99.0

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


@pytest.mark.asyncio
async def test_a_slow_io_loop_handoff_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread = katzen.AsyncioThread()
    thread.loop = asyncio.new_event_loop()
    runner = threading.Thread(target=thread.loop.run_forever, daemon=True)
    runner.start()

    async def work() -> str:
        return "done"

    caplog.set_level(logging.WARNING, logger="katzen")
    try:
        with monkeypatch.context() as patched:
            patched.setattr(katzen, "time", JumpingClock())
            result = await thread.run_in_io(work())
    finally:
        thread.loop.call_soon_threadsafe(thread.loop.stop)
        runner.join(timeout=5.0)
        thread.loop.close()

    assert result == "done"
    assert "io loop took 99.0s to start a run_in_io coroutine" in caplog.text


class FakeStatusSignal:
    def __init__(self) -> None:
        self.slots: list[object] = []

    def connect(self, slot: object) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: object) -> None:
        self.slots.remove(slot)


class SettlingQuickWidget:
    def __init__(self, statuses: list[QQuickWidget.Status]) -> None:
        self.statuses = statuses
        self.statusChanged = FakeStatusSignal()

    def status(self) -> QQuickWidget.Status:
        return self.statuses.pop(0)


@pytest.mark.asyncio
async def test_a_source_that_settles_while_connecting_needs_no_wait() -> None:
    widget = SettlingQuickWidget(
        [QQuickWidget.Status.Loading, QQuickWidget.Status.Ready],
    )
    await katzen._qml_source_ready(cast(QQuickWidget, widget))
    assert widget.statuses == []
    assert widget.statusChanged.slots == []


def test_a_composer_with_no_current_tab_keeps_its_height(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = QTabWidget()
    before = empty.maximumHeight()
    monkeypatch.setattr(window.ui, "singlemultitab", empty)

    window._fit_composer()

    assert empty.currentWidget() is None
    assert empty.maximumHeight() == before


@pytest.mark.asyncio
async def test_a_single_line_send_without_a_conversation_does_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        window,
        "_refuse_unless_joined",
        appending(sent, "asked"),
    )
    window.ui.chat_lineEdit.setText("never sent")
    monkeypatch.setattr(window, "convo_state", ignore)

    await window.chat_msg_single_line()

    assert window.ui.chat_lineEdit.text() == ""
    assert sent == []


@pytest.mark.asyncio
async def test_a_moved_unread_marker_is_persisted_on_a_refresh(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[tuple[int, int]] = []

    async def persist(conversation_id: int, order: int) -> None:
        saved.append((conversation_id, order))

    monkeypatch.setattr(network, "persist_first_unread", persist)
    state = loaded_window.convo_state()
    state.first_unread = 7

    await loaded_window._process_conversation_update(
        state.conversation_id,
        False,
    )

    assert saved == [(state.conversation_id, 0)]
    assert state.first_unread == 0


@pytest.fixture()
def popped(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []

    async def fake(menu: QMenu, global_pos: QPoint) -> None:
        shown.append("shown")
        return None

    monkeypatch.setattr(katzen, "_menu_chosen", fake)
    return shown


def peer_position(win: katzen.MainWindow, item: QStandardItem) -> QPoint:
    tree = win.ui.contacts_treeWidget
    proxy = proxy_of(win).mapFromSource(
        win.all_contacts.indexFromItem(item),
    )
    parent = proxy.parent()
    if parent.isValid():
        tree.expand(parent)
    point: QPoint = tree.visualRect(proxy).center()
    return point


def peer_named(win: katzen.MainWindow, name: str) -> QStandardItem:
    item = win.convo_state().contacts_standard_item
    for row in range(item.rowCount()):
        child: QStandardItem = item.child(row)
        if child.text() == name:
            return child
    raise AssertionError(f"no peer row named {name}")


class EmptyContacts:
    def __init__(self, real: object) -> None:
        self._real = real

    def itemFromIndex(self, index: QModelIndex) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.mark.asyncio
async def test_a_peer_row_with_no_backing_item_offers_no_menu(
    window: katzen.MainWindow,
    popped: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    pos = peer_position(window, peer_named(window, "bob"))

    with monkeypatch.context() as patched:
        patched.setattr(
            window,
            "all_contacts",
            EmptyContacts(
                window.all_contacts,
            ),
        )
        await window.peer_context_menu(pos)

    assert popped == []


@pytest.mark.asyncio
async def test_a_peer_row_without_a_read_cap_offers_no_menu(
    window: katzen.MainWindow,
    popped: list[str],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    bob = peer_named(window, "bob")
    assert getattr(bob, "peer_is_own") is False
    setattr(bob, "peer_read_cap_id", None)  # noqa: B010

    await window.peer_context_menu(peer_position(window, bob))

    assert popped == []


@pytest.mark.asyncio
async def test_a_transfer_row_without_an_id_offers_no_menu(
    window: katzen.MainWindow,
    popped: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = uuid.uuid4()
    window.transfers_model.start_transfer(stream, 1, "bob", 4)
    view = window.transfers_view
    view.resize(600, 200)
    pos = view.visualRect(view.model().index(0, 0)).center()
    real_data = window.transfers_model.data

    def data(index: QModelIndex, role: int = 0) -> object:
        if role == katzen.ROLE_TRANSFER_RCW_ID:
            return None
        return real_data(index, role)

    with monkeypatch.context() as patched:
        patched.setattr(window.transfers_model, "data", data)
        await window.transfers_context_menu(pos)

    assert popped == []


def conversation_index(
    win: katzen.MainWindow, conversation_id: int
) -> QModelIndex:
    state = win.conversation_state_by_id[conversation_id]
    source = win.all_contacts.indexFromItem(state.contacts_standard_item)
    index: QModelIndex = proxy_of(win).mapFromSource(source)
    return index


def peer_index(
    win: katzen.MainWindow,
    conversation_id: int,
    name: str,
) -> QModelIndex:
    parent = win.conversation_state_by_id[conversation_id]
    item = parent.contacts_standard_item
    for row in range(item.rowCount()):
        child = item.child(row)
        if child.text() == name:
            source = win.all_contacts.indexFromItem(child)
            index: QModelIndex = proxy_of(win).mapFromSource(source)
            return index
    raise AssertionError(f"no peer row named {name}")


@pytest.mark.asyncio
async def test_leaving_a_peer_row_persists_the_old_unread_marker(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[tuple[int, int]] = []

    async def persist(conversation_id: int, order: int) -> None:
        saved.append((conversation_id, order))

    monkeypatch.setattr(network, "persist_first_unread", persist)
    first_id = loaded_window.convo_state().conversation_id
    other = await seed_conversation(
        name="other room", own_name="me2", peers=("zoe",),
    )
    await add_seeded_conversation(loaded_window, other.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    saved.clear()
    old_state = loaded_window.conversation_state_by_id[other.conversation_id]
    old_state.first_unread = 7

    await loaded_window.conversation_selected(
        conversation_index(loaded_window, first_id),
        peer_index(loaded_window, other.conversation_id, "zoe"),
    )

    assert saved == [(other.conversation_id, 0)]
    assert old_state.first_unread == 0


@pytest.mark.asyncio
async def test_a_selection_with_no_conversation_state_stops_early(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = loaded_window.convo_state().conversation_id
    before = loaded_window.ui.ContactName.text()
    read_before = systray_of(loaded_window).read_messages
    selected = conversation_index(loaded_window, first_id)
    monkeypatch.setattr(loaded_window, "convo_state", ignore)

    await loaded_window.conversation_selected(selected, QModelIndex())

    assert loaded_window.ui.ContactName.text() == before
    assert systray_of(loaded_window).read_messages == read_before


@pytest.mark.asyncio
async def test_a_chatview_without_a_root_object_is_logged(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sources: list[str] = []

    async def ready(widget: object) -> None:
        return None

    first_id = loaded_window.convo_state().conversation_id
    selected = conversation_index(loaded_window, first_id)
    caplog.set_level(logging.WARNING, logger="katzen")

    with monkeypatch.context() as patched:
        patched.setattr(katzen, "_qml_source_ready", ready)
        patched.setattr(
            loaded_window.ui.qml_ChatLines,
            "rootObject",
            ignore,
        )
        patched.setattr(
            loaded_window.ui.qml_ChatLines,
            "setSource",
            sources.append,
        )
        await loaded_window.conversation_selected(selected, QModelIndex())

    assert sources == ["resources/chatview.qml"]
    assert "chatview.qml did not produce a root object" in caplog.text
