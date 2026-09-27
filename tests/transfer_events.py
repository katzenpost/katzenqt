"""Build a substream progress event in whichever shape network.py takes.

The queue carries bare tuples today and typed dataclasses once the union
lands; these helpers let a test name the event it means without pinning
either shape.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, cast

from katzenqt import network


def _typed(name: str) -> "Callable[..., Any] | None":
    """The dataclass network.py defines for this event, once it has one."""
    found = getattr(network, name, None)
    return cast("Callable[..., Any] | None", found)


def started(
    rcw_id: uuid.UUID, conversation_id: int, total: "int | None", name: str,
) -> Any:
    """A download that just began, with the row count it expects."""
    typed = _typed("TransferStarted")
    if typed is None:
        return ("started", rcw_id, conversation_id, total, name)
    return typed(rcw_id, conversation_id, total, name)


def piece(rcw_id: uuid.UUID, pieces: int, received_bytes: int) -> Any:
    """One more received piece of a download."""
    typed = _typed("TransferPiece")
    if typed is None:
        return ("piece", rcw_id, pieces, received_bytes)
    return typed(rcw_id, pieces, received_bytes)


def upload_started(
    rcw_id: uuid.UUID,
    conversation_id: int,
    total: "int | None",
    total_bytes: int,
    parent_name: str,
    basename: "str | None",
) -> Any:
    """An upload that just began, keyed by its indirection cap."""
    typed = _typed("UploadStarted")
    if typed is None:
        return (
            "upload_started", rcw_id, conversation_id, total, total_bytes,
            parent_name, basename,
        )
    return typed(
        rcw_id, conversation_id, total, total_bytes, parent_name, basename,
    )


def upload_piece(
    rcw_id: uuid.UUID, sent: int, remaining_bytes: int,
) -> Any:
    """One more sent piece of an upload."""
    typed = _typed("UploadPiece")
    if typed is None:
        return ("upload_piece", rcw_id, sent, remaining_bytes)
    return typed(rcw_id, sent, remaining_bytes)


def paused(rcw_id: uuid.UUID, direction: str, value: bool) -> Any:
    """A transfer the user paused, or resumed when value is False."""
    typed = _typed("TransferPaused")
    if typed is None:
        kinds = {
            ("download", True): "paused",
            ("download", False): "resumed",
            ("upload", True): "upload_paused",
            ("upload", False): "upload_resumed",
        }
        return (kinds[(direction, value)], rcw_id)
    return typed(rcw_id, direction, paused=value)


def completed(
    rcw_id: uuid.UUID, direction: str, cancelled: bool = False,
) -> Any:
    """A transfer that finished, or was cancelled when cancelled is True."""
    typed = _typed("TransferCompleted")
    if typed is None:
        if direction == "upload":
            return (
                "upload_cancelled" if cancelled else "upload_completed",
                rcw_id,
            )
        return ("completed", rcw_id)
    return typed(rcw_id, direction, cancelled=cancelled)


def failed(rcw_id: uuid.UUID, reason: str) -> Any:
    """A transfer that gave up, with the reason shown in the panel."""
    typed = _typed("TransferFailed")
    if typed is None:
        return ("failed", rcw_id, reason)
    return typed(rcw_id, reason)
