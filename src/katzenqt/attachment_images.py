"""Image-attachment helpers shared by the receive (network), send
(GUI), and render (qt_models) paths.

Kept headless-safe: only the stdlib and :mod:`persistent`
are imported at module load. The PySide6 ``QImage`` dependency used to
generate thumbnails is imported lazily inside
:func:`spill_image_thumbnail` so importers that never decode an image
(the integration runner, pytest collection without libEGL) do not pull
in the Qt GUI runtime.
"""
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import persistent

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

logger = logging.getLogger("katzen.attachment_images")

# Longest-edge cap for stored thumbnails, in pixels. Chosen to stay
# compact on disk and in the chat row while remaining legible.
THUMB_MAX_PX = 256

# JPEG quality used when writing thumbnails (0-100).
_THUMB_JPEG_QUALITY = 85

# Bound untrusted image decoding. A small compressed attachment can expand to
# an enormous raster (a decompression bomb), so cap decoded dimensions and the
# decoder allocation. The ceiling is DECODE_MAX_EDGE_PX squared at 4 bytes per
# RGBA pixel: 4096 * 4096 * 4 = 64 MiB. An out-of-process or seccomp decoder
# would be stronger but is not yet cross platform; keep this the single decode
# choke point until then.
DECODE_MAX_EDGE_PX = 4096
DECODE_ALLOC_LIMIT_MIB = DECODE_MAX_EDGE_PX * DECODE_MAX_EDGE_PX * 4 // (1024 * 1024)


def load_bounded_image(source: "Path | bytes") -> "QImage | None":
    """Decode an untrusted image with dimension and allocation caps.

    Returns a ``QImage``, or ``None`` when the source is undecodable, exceeds
    the pixel bound, or the Qt runtime is unavailable (headless)."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray
        from PySide6.QtGui import QImageReader
    except ImportError:
        logger.warning("Qt GUI runtime unavailable; skipping image decode")
        return None

    if isinstance(source, bytes):
        buffer = QBuffer()
        buffer.setData(QByteArray(source))
        buffer.open(QBuffer.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
    else:
        reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and (
        size.width() > DECODE_MAX_EDGE_PX or size.height() > DECODE_MAX_EDGE_PX
    ):
        return None
    previous_limit = QImageReader.allocationLimit()
    QImageReader.setAllocationLimit(DECODE_ALLOC_LIMIT_MIB)
    try:
        image = reader.read()
    finally:
        QImageReader.setAllocationLimit(previous_limit)
    return None if image.isNull() else image


def is_image_attachment(filetype: "str | None", basename: str) -> bool:
    """Whether an attachment should be rendered as an inline thumbnail.

    Trusts an ``image/*`` ``filetype`` tag first, then falls back to the
    basename's extension so legacy rows (tagged ``arbitrary``) and
    received files still resolve."""
    if filetype and filetype.startswith("image/"):
        return True
    guessed, _ = mimetypes.guess_type(basename)
    return bool(guessed and guessed.startswith("image/"))


def guess_image_filetype(path: Path) -> str:
    """Return an ``image/*`` MIME type for recognised image files, else
    ``arbitrary``. Non-image types are intentionally collapsed to the
    generic marker so only images trigger thumbnail rendering."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "arbitrary"


def spill_image_thumbnail(
    *,
    conversation_id: int,
    file_uuid: uuid.UUID,
    safe_basename: str,
    source: "Path | bytes",
) -> "str | None":
    """Generate a scaled JPEG thumbnail next to the full attachment and
    return its state-dir-relative path, or ``None`` if the source is not
    a decodable image (or the Qt GUI runtime is unavailable).

    The thumbnail is written exclusively at mode ``0o600`` under
    ``attachments/{conversation_id}/`` so it shares the lifecycle and
    permissions of the full file spilled by
    :func:`network._spill_attachment`."""
    image = load_bounded_image(source)
    if image is None:
        return None
    from PySide6.QtCore import Qt, QBuffer

    # Only downscale: a source already within the box is stored as-is so
    # small images are not blurrily upscaled.
    if image.width() > THUMB_MAX_PX or image.height() > THUMB_MAX_PX:
        scaled = image.scaled(
            THUMB_MAX_PX,
            THUMB_MAX_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    else:
        scaled = image

    # Encode to JPEG bytes first so the file is written through a single
    # exclusive-create fd, matching network._spill_attachment. QBuffer
    # manages its own internal byte array here; passing a temporary
    # QByteArray would leave a dangling reference and crash under PySide6.
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    # PySide6 stubs omit the (QIODevice, format, quality) save() overload.
    if not scaled.save(buffer, "JPEG", _THUMB_JPEG_QUALITY):  # type: ignore[call-overload]
        logger.warning("failed to encode thumbnail for %s", safe_basename)  # pragma: no cover
        return None  # pragma: no cover
    jpeg_bytes = buffer.data().data()

    conv_dir = persistent.state_file.parent / "attachments" / str(conversation_id)
    conv_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = f"{file_uuid}-thumb-{safe_basename}.jpg"
    rel_path = f"attachments/{conversation_id}/{filename}"
    abs_path = persistent.state_file.parent / rel_path

    fd = os.open(str(abs_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, jpeg_bytes)
    finally:
        os.close(fd)

    return rel_path
