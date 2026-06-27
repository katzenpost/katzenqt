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

from . import persistent

logger = logging.getLogger("katzen.attachment_images")

# Longest-edge cap for stored thumbnails, in pixels. Chosen to stay
# compact on disk and in the chat row while remaining legible.
THUMB_MAX_PX = 256

# JPEG quality used when writing thumbnails (0-100).
_THUMB_JPEG_QUALITY = 85


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
    try:
        from PySide6.QtCore import Qt, QBuffer
        from PySide6.QtGui import QImage
    except ImportError:
        logger.warning("Qt GUI runtime unavailable; skipping thumbnail")
        return None

    image = QImage()
    if isinstance(source, bytes):
        loaded = image.loadFromData(source)
    else:
        loaded = image.load(str(source))
    if not loaded or image.isNull():
        return None

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
    if not scaled.save(buffer, "JPEG", _THUMB_JPEG_QUALITY):
        logger.warning("failed to encode thumbnail for %s", safe_basename)
        return None
    jpeg_bytes = bytes(buffer.data())

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
