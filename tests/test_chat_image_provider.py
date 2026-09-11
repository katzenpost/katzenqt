"""Tests for ``qt_models.ChatImageProvider``: serving inline chat thumbnails
from state-dir-relative paths, with a path-traversal guard."""
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSize  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from katzenqt import persistent  # noqa: E402
from katzenqt.attachment_images import THUMB_MAX_PX  # noqa: E402
from katzenqt.qt_models import ChatImageProvider  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def _write_png(path: Path, width: int, height: int) -> None:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF112233)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path))


def test_request_image_serves_a_thumbnail() -> None:
    rel = "attachments/1/thumb.png"
    _write_png(persistent.state_file.parent / rel, 10, 10)
    image = ChatImageProvider().requestImage(rel, QSize(), QSize())
    assert not image.isNull()
    assert (image.width(), image.height()) == (10, 10)


def test_request_image_empty_path_is_null() -> None:
    assert ChatImageProvider().requestImage("", QSize(), QSize()).isNull()


def test_request_image_rejects_path_traversal() -> None:
    image = ChatImageProvider().requestImage("../../etc/passwd", QSize(), QSize())
    assert image.isNull()


def test_request_image_missing_file_is_null() -> None:
    image = ChatImageProvider().requestImage(
        "attachments/1/does-not-exist.png", QSize(), QSize()
    )
    assert image.isNull()


def test_request_image_downscales_a_large_image() -> None:
    rel = "attachments/2/big.png"
    _write_png(persistent.state_file.parent / rel, THUMB_MAX_PX * 2, THUMB_MAX_PX)
    image = ChatImageProvider().requestImage(rel, QSize(), QSize())
    assert not image.isNull()
    assert max(image.width(), image.height()) <= THUMB_MAX_PX
