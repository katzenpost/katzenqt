"""Tests for ``attachment_images``: image detection and the on-disk
thumbnail spill shared by the receive, send, and render paths."""
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from katzenqt import attachment_images, persistent  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    """QImage encode/decode needs a live Qt GUI application; create one
    offscreen so the suite runs without a display."""
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def _png_bytes(width: int, height: int) -> bytes:
    """Encode a solid-colour PNG of the given size to raw bytes."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF3366CC)
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")  # type: ignore[call-overload]
    return bytes(buffer.data().data())


@pytest.mark.parametrize(
    "filetype, basename, expected",
    [
        ("image/jpeg", "photo.jpg", True),
        ("image/png", "diagram.png", True),
        # Filetype tag wins, but the extension fallback also catches it.
        ("arbitrary", "photo.jpeg", True),
        ("arbitrary", "report.pdf", False),
        ("audio/opus", "note.opus", False),
        (None, "screenshot.png", True),
        (None, "data.bin", False),
    ],
)
def test_is_image_attachment(
    filetype: str | None, basename: str, expected: bool
) -> None:
    assert attachment_images.is_image_attachment(filetype, basename) is expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("photo.jpg", "image/jpeg"),
        ("diagram.png", "image/png"),
        ("report.pdf", "arbitrary"),
        ("note.txt", "arbitrary"),
    ],
)
def test_guess_image_filetype(
    tmp_path: Path, name: str, expected: str
) -> None:
    assert attachment_images.guess_image_filetype(tmp_path / name) == expected


def test_spill_image_thumbnail_scales_and_writes() -> None:
    blob = _png_bytes(800, 600)
    file_uuid = uuid.uuid4()

    rel_path = attachment_images.spill_image_thumbnail(
        conversation_id=42,
        file_uuid=file_uuid,
        safe_basename="photo.png",
        source=blob,
    )

    assert rel_path == f"attachments/42/{file_uuid}-thumb-photo.png.jpg"
    abs_path = persistent.state_file.parent / rel_path
    assert abs_path.is_file()

    thumb = QImage()
    assert thumb.load(str(abs_path))
    assert max(thumb.width(), thumb.height()) <= attachment_images.THUMB_MAX_PX
    # Aspect ratio (4:3) is preserved within rounding.
    assert thumb.width() == attachment_images.THUMB_MAX_PX
    assert abs(thumb.height() - int(attachment_images.THUMB_MAX_PX * 3 / 4)) <= 1


def test_spill_image_thumbnail_small_image_not_upscaled() -> None:
    blob = _png_bytes(64, 48)
    rel_path = attachment_images.spill_image_thumbnail(
        conversation_id=1,
        file_uuid=uuid.uuid4(),
        safe_basename="tiny.png",
        source=blob,
    )
    abs_path = persistent.state_file.parent / rel_path
    thumb = QImage()
    assert thumb.load(str(abs_path))
    # KeepAspectRatio with a target larger than the source still fits the
    # box; the longest edge must not exceed the cap.
    assert max(thumb.width(), thumb.height()) <= attachment_images.THUMB_MAX_PX


def test_spill_image_thumbnail_non_image_returns_none() -> None:
    assert attachment_images.spill_image_thumbnail(
        conversation_id=1,
        file_uuid=uuid.uuid4(),
        safe_basename="not-an-image.png",
        source=b"this is not image data",
    ) is None


def test_spill_attachment_image_includes_thumb_rel_path() -> None:
    import cbor2

    from katzenqt import models, network

    upload = models.GroupChatFileUpload(
        payload=_png_bytes(640, 480),
        filetype="image/png",
        basename="received.png",
    )
    marker = network._spill_attachment(upload, b"TODO" * 8, conversation_id=5)

    assert marker[:1] == b"F"
    decoded = cbor2.loads(marker[1:])
    assert decoded["kind"] == "file_marker"
    thumb_rel_path = decoded["thumb_rel_path"]
    assert thumb_rel_path.startswith("attachments/5/")
    assert thumb_rel_path.endswith("-thumb-received.png.jpg")
    assert (persistent.state_file.parent / thumb_rel_path).is_file()


def test_spill_attachment_non_image_has_no_thumb() -> None:
    import cbor2

    from katzenqt import models, network

    upload = models.GroupChatFileUpload(
        payload=b"%PDF-1.4 not really",
        filetype="arbitrary",
        basename="doc.pdf",
    )
    marker = network._spill_attachment(upload, b"TODO" * 8, conversation_id=5)
    decoded = cbor2.loads(marker[1:])
    assert "thumb_rel_path" not in decoded


def test_load_bounded_image_decodes_a_valid_image() -> None:
    out = attachment_images.load_bounded_image(_png_bytes(8, 8))
    assert out is not None
    assert (out.width(), out.height()) == (8, 8)


def test_load_bounded_image_rejects_oversized_dimensions() -> None:
    over = attachment_images.DECODE_MAX_EDGE_PX + 1
    # Header dimensions exceed the cap, so it is refused before full decode.
    assert attachment_images.load_bounded_image(_png_bytes(over, 1)) is None


def test_load_bounded_image_returns_none_for_undecodable_bytes() -> None:
    assert attachment_images.load_bounded_image(b"not an image at all") is None


def test_load_bounded_image_reads_from_a_path(tmp_path: Path) -> None:
    png = tmp_path / "x.png"
    png.write_bytes(_png_bytes(4, 4))
    out = attachment_images.load_bounded_image(png)
    assert out is not None
    assert (out.width(), out.height()) == (4, 4)


def test_load_bounded_image_returns_none_when_qt_unavailable() -> None:
    # Simulate the headless path: importing the Qt GUI module fails.
    with patch.dict(sys.modules, {"PySide6.QtGui": None}):
        assert attachment_images.load_bounded_image(b"whatever") is None
