import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QFile  # noqa: E402

from katzenqt import resources_rc  # noqa: E402

A_BUNDLED_FILE = ":/icons/echomix_icon.svg"


def test_the_bundled_resources_are_registered() -> None:
    assert QFile(A_BUNDLED_FILE).exists()


def test_cleanup_unregisters_and_init_puts_them_back() -> None:
    resources_rc.qCleanupResources()
    try:
        assert not QFile(A_BUNDLED_FILE).exists()
    finally:
        resources_rc.qInitResources()
    assert QFile(A_BUNDLED_FILE).exists()
