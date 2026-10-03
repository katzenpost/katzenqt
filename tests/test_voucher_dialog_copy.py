from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
