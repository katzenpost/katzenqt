from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from katzenqt import katzen  # noqa: E402

from tests.test_katzen_gui_common import qt_app  # noqa: E402,F401


def test_copy_code_copies_the_voucher_and_confirms_on_the_button(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-abc123")
    assert dialog.copy_button.text() == "Copy voucher"
    dialog.copy_code()
    assert QApplication.clipboard().text() == "voucher-abc123"
    assert dialog.copy_button.text() == "Copied"


def test_the_copy_button_returns_to_its_label(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-abc123")
    dialog.copy_code()
    assert dialog.copy_button.text() == "Copied"
    dialog.restore_copy_label()
    assert dialog.copy_button.text() == "Copy voucher"


def test_the_voucher_leaves_the_clipboard_again(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-abc123")
    dialog.copy_code()
    assert QApplication.clipboard().text() == "voucher-abc123"
    katzen.clear_secret("voucher-abc123")
    assert QApplication.clipboard().text() == ""


def test_something_copied_since_is_left_alone(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-abc123")
    dialog.copy_code()
    QApplication.clipboard().setText("a shopping list")
    katzen.clear_secret("voucher-abc123")
    assert QApplication.clipboard().text() == "a shopping list"


def _fire_the_clear(qt_app: QApplication) -> None:
    timer = qt_app.findChild(QTimer, katzen.SECRET_TIMER_NAME)
    assert timer is not None, "no clear was scheduled"
    assert timer.isActive(), "the scheduled clear was dropped"
    timer.timeout.emit()


def test_the_clear_survives_the_dialog_closing(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-closes")
    dialog.copy_code()
    dialog.close()
    dialog.deleteLater()
    qt_app.processEvents()
    assert QApplication.clipboard().text() == "voucher-closes"
    _fire_the_clear(qt_app)
    assert QApplication.clipboard().text() == ""


def test_a_second_copy_is_not_cleared_by_the_first_timer(
    qt_app: QApplication,
) -> None:
    parent = QWidget()
    dialog = katzen.VoucherDialog(parent, "voucher-one")
    dialog.copy_code()
    katzen.copy_secret("voucher-two")
    _fire_the_clear(qt_app)
    assert QApplication.clipboard().text() == ""
    katzen.copy_secret("voucher-three")
    katzen.clear_secret("voucher-two")
    assert QApplication.clipboard().text() == "voucher-three"


def test_quitting_takes_a_pending_secret_with_it(
    qt_app: QApplication,
) -> None:
    katzen.copy_secret("voucher-at-quit")
    assert QApplication.clipboard().text() == "voucher-at-quit"
    qt_app.aboutToQuit.emit()
    assert QApplication.clipboard().text() == ""


def test_the_copy_still_happens_with_no_application_to_schedule_on(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a QApplication there is nothing to own the timer, so the
    clear is best effort: the copy still reaches the clipboard."""
    monkeypatch.setattr(
        katzen, "QApplication",
        SimpleNamespace(
            instance=lambda: None,
            clipboard=QApplication.clipboard,
        ),
    )
    katzen.copy_secret("voucher-no-app")
    assert QApplication.clipboard().text() == "voucher-no-app"
