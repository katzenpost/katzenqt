from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent, QFont, QIcon, QKeyEvent  # noqa: E402
from PySide6.QtNetwork import (  # noqa: E402
    QHostAddress,
    QNetworkAccessManager,
    QTcpServer,
)
from PySide6.QtQml import QQmlEngine  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QToolButton,
)

from katzenqt import katzen, network, persistent, theme  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    FakeIoThread,
    FakeMessageBox,
    boxes,
    qt_app,
    window,
)
from tests.stubs import appending, call_now, returning


class FakeFontDialog:
    chosen: QFont | None = None

    @staticmethod
    def getFont() -> tuple[bool, QFont]:
        font = FakeFontDialog.chosen
        return (font is not None, font or QFont())


class FakeThemeDialog:
    opened: list[object] = []

    def __init__(self, manager: object, parent: object) -> None:
        self.manager = manager
        self.parent_widget = parent

    def exec(self) -> int:
        FakeThemeDialog.opened.append(self.manager)
        return 1


def test_pending_vouchers_dialog_lists_every_row(
    window: katzen.MainWindow,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    dialog = katzen.PendingVouchersDialog(window, [
        (first, "room one", "joiner", "minted"),
        (second, "room two", "inductor", "replied"),
    ])
    assert dialog.windowTitle() == "Pending vouchers"
    assert dialog.list_widget.count() == 2
    assert dialog.list_widget.item(0).text() == "room one   [joiner, minted]"
    assert dialog.list_widget.item(1).text() == (
        "room two   [inductor, replied]"
    )
    assert dialog.list_widget.item(0).data(
        Qt.ItemDataRole.UserRole,
    ) == first
    assert dialog.cancelled == []
    dialog.deleteLater()


def test_cancelling_a_voucher_removes_its_row(
    window: katzen.MainWindow,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    dialog = katzen.PendingVouchersDialog(window, [
        (first, "room one", "joiner", "minted"),
        (second, "room two", "joiner", "minted"),
    ])
    dialog.list_widget.setCurrentRow(1)
    dialog._cancel_selected()
    assert dialog.cancelled == [second]
    assert dialog.list_widget.count() == 1
    assert dialog.list_widget.item(0).text().startswith("room one")
    dialog.deleteLater()


def test_cancelling_without_a_selection_is_a_no_op(
    window: katzen.MainWindow,
) -> None:
    dialog = katzen.PendingVouchersDialog(window, [])
    dialog._cancel_selected()
    assert dialog.cancelled == []
    dialog.deleteLater()


def test_the_stats_dialog_runs_its_timer_only_while_visible(
    window: katzen.MainWindow,
) -> None:
    dialog = katzen.StatsDialog(window)
    assert dialog._timer.isActive() is False
    dialog.show()
    assert dialog._timer.isActive() is True
    assert dialog._timer.interval() == 1000
    dialog.hide()
    assert dialog._timer.isActive() is False
    dialog.deleteLater()


def test_the_packets_dialog_runs_its_timer_only_while_visible(
    window: katzen.MainWindow,
) -> None:
    dialog = katzen.PacketsDialog(window)
    assert dialog._timer.isActive() is False
    dialog.show()
    assert dialog._timer.isActive() is True
    dialog.hide()
    assert dialog._timer.isActive() is False
    dialog.deleteLater()


@pytest.mark.asyncio
async def test_the_consensus_dialog_runs_its_timer_only_while_visible(
    window: katzen.MainWindow,
) -> None:
    async def fetch() -> None:
        return None

    dialog = katzen.ConsensusDialog(window, fetch)
    assert dialog._timer.isActive() is False
    assert dialog._timer.interval() == 5000
    dialog.show()
    assert dialog._timer.isActive() is True
    dialog.hide()
    assert dialog._timer.isActive() is False
    for _ in range(5):
        await asyncio.sleep(0)
    dialog.deleteLater()


@pytest.mark.asyncio
async def test_the_consensus_dialog_refresh_schedules_a_fetch(
    window: katzen.MainWindow,
) -> None:
    calls: list[int] = []

    async def fetch() -> None:
        calls.append(1)
        return None

    dialog = katzen.ConsensusDialog(window, fetch)
    dialog.refresh()
    for _ in range(10):
        await asyncio.sleep(0)
    assert calls == [1]
    assert dialog._fields["epoch"].text() == "no PKI document yet"
    dialog.deleteLater()


@pytest.mark.asyncio
async def test_a_failing_fetch_reports_an_unavailable_document(
    window: katzen.MainWindow,
) -> None:
    async def fetch() -> None:
        raise ConnectionError("daemon down")

    dialog = katzen.ConsensusDialog(window, fetch)
    await dialog._refresh_async()
    assert dialog._fields["epoch"].text() == "PKI document unavailable"
    assert dialog._tree.topLevelItemCount() == 0
    dialog.deleteLater()


@pytest.mark.asyncio
async def test_an_unchanged_epoch_does_not_rebuild_the_tree(
    window: katzen.MainWindow,
) -> None:
    doc = {
        "Epoch": 4242,
        "GenesisEpoch": 4000,
        "GatewayNodes": [],
        "ServiceNodes": [],
        "StorageReplicas": [],
        "Topology": [],
    }

    async def fetch() -> dict[str, object]:
        return doc

    dialog = katzen.ConsensusDialog(window, fetch)
    await dialog._refresh_async()
    first_count = dialog._tree.topLevelItemCount()
    marker = katzen.QTreeWidgetItem(["sentinel", ""])
    dialog._tree.addTopLevelItem(marker)
    await dialog._refresh_async()
    assert dialog._last_epoch == 4242
    assert dialog._tree.topLevelItemCount() == first_count + 1
    assert dialog._fields["genesis"].text() == "4000"
    dialog.deleteLater()


def test_show_stats_creates_the_window_once(
    window: katzen.MainWindow,
) -> None:
    window.show_stats()
    first = window.stats_dialog
    window.show_stats()
    assert window.stats_dialog is first
    assert first.isVisible() is True
    first.hide()


def test_show_packets_creates_the_window_once(
    window: katzen.MainWindow,
) -> None:
    window.show_packets()
    first = window.packets_dialog
    window.show_packets()
    assert window.packets_dialog is first
    first.hide()


@pytest.mark.asyncio
async def test_show_consensus_wires_a_fetch_through_the_io_thread(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = {"Epoch": 7}

    async def get_pki(connection: object) -> dict[str, int]:
        assert connection is window.iothread.kp_client
        return doc

    monkeypatch.setattr(network, "get_pki_document", get_pki)
    window.show_consensus()
    first = window.consensus_dialog
    window.show_consensus()
    assert window.consensus_dialog is first
    assert await first._fetch() == doc
    first.hide()
    for _ in range(10):
        await asyncio.sleep(0)


def test_closing_without_really_quit_keeps_the_app(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdowns: list[int] = []
    quits: list[int] = []

    def record_shutdown() -> None:
        shutdowns.append(1)

    def record_quit(self: object) -> None:
        quits.append(1)

    monkeypatch.setattr(network, "shutdown", record_shutdown)
    window.app = type("RecordingApp", (), {"quit": record_quit})()
    systray = window.systray
    window.close()
    assert shutdowns == []
    assert quits == []
    assert window.systray is systray


def test_closing_with_really_quit_shuts_the_network_down(
    window: katzen.MainWindow,
) -> None:
    quits: list[int] = []
    window.app = type(
        "RecordingApp", (), {"quit": appending(quits, 1)},
    )()
    window.close(really_quit=True)
    assert window.systray is False
    assert quits == [1]
    assert getattr(network, "__should_quit").is_set() is True


def test_the_close_event_hides_the_window_into_the_tray(
    window: katzen.MainWindow,
) -> None:
    window.show()
    event = QCloseEvent()
    event.accept()
    window.closeEvent(event)
    assert event.isAccepted() is False
    assert window.isVisible() is False
    assert window.systray.messages == [
        ("Still running", f"{katzen.APP_NAME} running in background."),
    ]


def test_the_close_event_without_a_tray_is_accepted(
    window: katzen.MainWindow,
) -> None:
    window.systray = None
    event = QCloseEvent()
    event.accept()
    window.closeEvent(event)
    assert event.isAccepted() is True


def test_error_and_exit_reports_and_raises(
    qt_app: QApplication, boxes: type[FakeMessageBox],
) -> None:
    holder = QMainWindow()
    with pytest.raises(SystemExit) as caught:
        katzen.error_and_exit(qt_app, "the daemon is missing", holder)
    assert caught.value.code == "the daemon is missing"
    assert boxes.seen == [
        ("critical", f"ERROR: {katzen.APP_NAME}", "the daemon is missing"),
    ]
    holder.deleteLater()


def test_error_and_exit_makes_its_own_window_when_given_none(
    qt_app: QApplication, boxes: type[FakeMessageBox],
) -> None:
    with pytest.raises(SystemExit):
        katzen.error_and_exit(qt_app, "no state file")
    assert boxes.texts() == ["no state file"]


def test_the_backend_relays_its_update_signal(qt_app: QApplication) -> None:
    backend = katzen.Backend()
    seen: list[str] = []
    backend.updated.connect(seen.append)
    backend.set_convo_model_and_scroll()
    assert seen == ["a"]


def _pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)


def test_the_firewall_access_manager_refuses_to_connect(
    qt_app: QApplication,
) -> None:
    server = QTcpServer()
    assert server.listen(QHostAddress.SpecialAddress.LocalHost, 0) is True
    port = server.serverPort()
    accepted: list[int] = []

    def count_connection() -> None:
        accepted.append(1)

    server.newConnection.connect(count_connection)
    engine = QQmlEngine()
    factory = katzen.FirewallNetworkAccessManagerFactory()
    engine.setNetworkAccessManagerFactory(factory)
    parent = katzen.QObject()
    manager = factory.create(parent)
    try:
        assert engine.networkAccessManagerFactory() is factory
        assert isinstance(
            engine.networkAccessManager(),
            katzen.FirewallNetworkAccessManager,
        )
        assert isinstance(manager, katzen.FirewallNetworkAccessManager)
        assert manager.parent() is parent

        control = QNetworkAccessManager()
        control.connectToHost("127.0.0.1", port)
        deadline = time.monotonic() + 5.0
        while not accepted and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.002)
        assert accepted == [1]

        assert manager.connectToHost("127.0.0.1", port) is None
        assert manager.connectToHostEncrypted("127.0.0.1", port) is None
        _pump(qt_app, 0.3)
        assert accepted == [1]
    finally:
        engine.setNetworkAccessManagerFactory(None)
        server.close()
        parent.deleteLater()


def test_the_key_press_filter_never_eats_an_event(
    qt_app: QApplication,
) -> None:
    filt = katzen.KeyPressFilter()
    key = QKeyEvent(
        katzen.QEvent.Type.KeyPress, Qt.Key.Key_Space,
        Qt.KeyboardModifier.NoModifier, " ",
    )
    other = katzen.QEvent(katzen.QEvent.Type.None_)
    assert filt.eventFilter(None, key) is False
    assert filt.eventFilter(None, other) is False


def test_the_placeholder_helpers_are_inert(qt_app: QApplication) -> None:
    assert katzen.todo_keys_push_to_talk() is None
    assert katzen.todo_settings() is None
    assert katzen.rebuild_pydantic_models() is None


def test_todo_keys_still_names_a_key_that_pyside_dropped(
    qt_app: QApplication,
) -> None:
    with pytest.raises(AttributeError) as caught:
        katzen.todo_keys()
    assert "FindNextPrevious" in str(caught.value)


def test_unread_tracking_is_not_implemented_yet(
    window: katzen.MainWindow,
) -> None:
    assert window.do_we_even_have_unread_messages() is False


def test_an_action_error_is_logged_and_queued(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        katzen.QTimer, "singleShot",
        staticmethod(call_now),
    )
    caplog.set_level(logging.ERROR, logger="katzen")
    window._show_action_error("Could not send your vote", ValueError("nope"))
    assert "Could not send your vote: nope" in caplog.text
    assert boxes.seen[0].text == "Could not send your vote:\nnope"


@pytest.mark.asyncio
async def test_dialog_finished_resolves_with_the_result_code(
    window: katzen.MainWindow,
) -> None:
    dialog = QDialog(window)
    task = asyncio.ensure_future(katzen._dialog_finished(dialog))
    for _ in range(5):
        await asyncio.sleep(0)
    dialog.done(3)
    assert await task == 3
    dialog.deleteLater()


@pytest.mark.asyncio
async def test_menu_chosen_returns_the_triggered_action(
    window: katzen.MainWindow,
) -> None:
    menu = QMenu(window)
    action = menu.addAction("Pause download")
    task = asyncio.ensure_future(katzen._menu_chosen(menu, QPoint(0, 0)))
    for _ in range(5):
        await asyncio.sleep(0)
    menu.triggered.emit(action)
    menu.aboutToHide.emit()
    assert await task is action
    menu.deleteLater()


@pytest.mark.asyncio
async def test_a_dismissed_menu_returns_nothing(
    window: katzen.MainWindow,
) -> None:
    menu = QMenu(window)
    menu.addAction("Resume download")
    task = asyncio.ensure_future(katzen._menu_chosen(menu, QPoint(0, 0)))
    for _ in range(5):
        await asyncio.sleep(0)
    menu.aboutToHide.emit()
    menu.aboutToHide.emit()
    assert await task is None
    menu.deleteLater()


def test_the_font_dialog_is_built_once_and_reused(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(katzen, "QFontDialog", FakeFontDialog)
    window.font_settings_dialog()
    first = window.font_settings_qdialog
    window.font_settings_dialog()
    assert window.font_settings_qdialog is first
    buttons = [c for c in first.children() if isinstance(c, QToolButton)]
    assert len(buttons) == 3
    first.hide()


def test_a_stored_font_choice_is_restored_onto_the_button(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(katzen, "QFontDialog", FakeFontDialog)
    window.settings = {
        "contactName.font.family": "Courier",
        "contactName.font.pointSize": 14,
    }
    window.font_settings_dialog()
    dialog = window.font_settings_qdialog
    button = next(
        c for c in dialog.children()
        if isinstance(c, QToolButton)
        and c.objectName() == "contactName_toolButton"
    )
    assert button.text() == "Courier 14"
    dialog.hide()


def test_picking_a_font_persists_it(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(katzen, "QFontDialog", FakeFontDialog)
    FakeFontDialog.chosen = QFont("Courier", 18)
    window.settings = {}
    window.font_settings_dialog()
    dialog = window.font_settings_qdialog
    button = next(
        c for c in dialog.children()
        if isinstance(c, QToolButton)
        and c.objectName() == "messageText_toolButton"
    )
    button.click()
    FakeFontDialog.chosen = None
    assert button.text() == "Courier 18"
    assert window.settings["messageText.font.family"] == "Courier"
    assert window.settings["messageText.font.pointSize"] == 18
    with persistent.Session(persistent._engine_sync) as sess:
        family = sess.get(persistent.AppSetting, "messageText.font.family")
        size = sess.get(persistent.AppSetting, "messageText.font.pointSize")
        assert family is not None and family.value == "Courier"
        assert family.type == "str"
        assert size is not None and size.type == "int"
    dialog.hide()


def test_cancelling_the_font_picker_changes_nothing(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(katzen, "QFontDialog", FakeFontDialog)
    FakeFontDialog.chosen = None
    window.settings = {}
    window.font_settings_dialog()
    dialog = window.font_settings_qdialog
    button = next(c for c in dialog.children() if isinstance(c, QToolButton))
    before = button.text()
    button.click()
    assert button.text() == before
    assert window.settings == {}
    dialog.hide()


def test_a_broken_database_only_costs_the_saved_font(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(katzen, "QFontDialog", FakeFontDialog)
    FakeFontDialog.chosen = QFont("Serif", 9)
    window.settings = {}
    window.font_settings_dialog()
    dialog = window.font_settings_qdialog

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("disk full")

    caplog.set_level(logging.WARNING, logger="katzen")
    button = next(c for c in dialog.children() if isinstance(c, QToolButton))
    with monkeypatch.context() as scoped:
        scoped.setattr(persistent, "Session", boom)
        button.click()
    FakeFontDialog.chosen = None
    assert "could not persist font setting: disk full" in caplog.text
    assert window.settings["contactName.font.family"] == "Serif"
    dialog.hide()


def test_the_theme_dialog_is_opened_with_the_window_manager(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeThemeDialog.opened = []
    monkeypatch.setattr(theme, "ThemeDialog", FakeThemeDialog)
    window.theme_settings_dialog()
    assert FakeThemeDialog.opened == [window.theme]


def test_the_systray_icon_mirrors_the_unread_state(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(returning(True)),
    )
    window.systray = None
    window.echomix_icon = QIcon()
    window.echomix_icon_new_message = QIcon()
    tray = katzen.MixSystrayIcon(window, window.echomix_icon)
    assert window.systray is tray
    assert tray.mw is window
    assert tray.toolTip() == katzen.APP_NAME
    assert tray.contextMenu().actions() == [window.ui.action_quit]

    tray.has_new_messages()
    tray.has_read_messages()
    tray.showMessage("hello", "world")
    tray.on_left_click(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.isVisible() is True
    assert tray.messageClicked() is None
    tray.hide()
    window.systray = None


def test_the_systray_icon_gives_up_without_a_tray(
    window: katzen.MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(returning(False)),
    )
    systray = window.systray
    tray = katzen.MixSystrayIcon(window, QIcon())
    assert window.systray is systray
    assert tray.isVisible() is False


@pytest.mark.asyncio
async def test_the_io_thread_reports_a_failed_tally_reconcile(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom() -> None:
        raise RuntimeError("log unreadable")

    monkeypatch.setattr(
        katzen.tally_controller.INSTANCE, "reconcile_from_log", boom,
    )
    caplog.set_level(logging.ERROR, logger="katzen")
    thread = katzen.AsyncioThread()
    await thread.reconcile_tally_once()
    assert "startup tally reconcile failed: log unreadable" in caplog.text


@pytest.mark.asyncio
async def test_the_io_thread_reconciles_the_tally_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def reconcile() -> None:
        calls.append(1)

    monkeypatch.setattr(
        katzen.tally_controller.INSTANCE, "reconcile_from_log", reconcile,
    )
    thread = katzen.AsyncioThread()
    await thread.reconcile_tally_once()
    assert calls == [1]


@pytest.mark.asyncio
async def test_warming_the_engine_always_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def warm() -> None:
        return None

    monkeypatch.setattr(persistent, "warm_async_engine", warm)
    thread = katzen.AsyncioThread()
    assert thread.engine_warmed.is_set() is False
    await thread.warm_engine()
    assert thread.engine_warmed.is_set() is True


@pytest.mark.asyncio
async def test_a_failed_warm_up_still_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def warm() -> None:
        raise RuntimeError("no database")

    monkeypatch.setattr(persistent, "warm_async_engine", warm)
    thread = katzen.AsyncioThread()
    with pytest.raises(RuntimeError):
        await thread.warm_engine()
    assert thread.engine_warmed.is_set() is True


@pytest.mark.asyncio
async def test_async_main_retries_until_the_daemon_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    attempts: list[int] = []

    async def reconnect() -> object:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise ConnectionRefusedError("kpclientd is not up")
        return client

    monkeypatch.setattr(network, "reconnect", reconnect)
    thread = katzen.AsyncioThread()
    await thread.async_main()
    assert thread.kp_client is client
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_run_in_io_hands_work_to_the_other_loop() -> None:
    import threading

    thread = katzen.AsyncioThread()
    thread.loop = asyncio.new_event_loop()
    runner = threading.Thread(target=thread.loop.run_forever, daemon=True)
    runner.start()

    async def work() -> str:
        return f"ran on {id(asyncio.get_running_loop())}"

    try:
        result = await thread.run_in_io(work())
        assert result == f"ran on {id(thread.loop)}"
    finally:
        thread.loop.call_soon_threadsafe(thread.loop.stop)
        runner.join(timeout=5.0)
        thread.loop.close()


@pytest.mark.asyncio
async def test_waiting_for_a_warmed_engine_returns_when_it_is_set() -> None:
    thread = katzen.AsyncioThread()
    thread.engine_warmed.set()
    await katzen._wait_for_engine_warmed(thread)
    assert thread.engine_warmed.is_set() is True


@pytest.mark.asyncio
async def test_waiting_gives_up_on_a_dead_io_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread = FakeIoThread()
    thread.engine_warmed.clear()
    thread.alive = False
    caplog.set_level(logging.ERROR, logger="katzen")
    await katzen._wait_for_engine_warmed(thread)
    assert "io thread exited before it warmed the async engine" in caplog.text


def test_the_resolved_attachment_defaults_to_received() -> None:
    resolved = katzen._ResolvedAttachment("a.txt", "text/plain", Path("/a.txt"))
    assert resolved.received is True
    assert resolved.basename == "a.txt"
