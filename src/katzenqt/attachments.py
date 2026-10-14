"""Attachment files a conversation's log refers to, and deleting the ones
no message refers to any more."""

from __future__ import annotations

import logging
from pathlib import Path

import cbor2
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import persistent

logger = logging.getLogger("katzen.attachments")


def paths_in(payload: bytes) -> "set[str]":
    """The attachment files a log row's payload refers to."""
    if payload[:1] != b"F":
        return set()
    try:
        decoded = cbor2.loads(payload[1:])
    except Exception:
        return set()
    if not isinstance(decoded, dict):
        return set()
    keys = ("rel_path", "thumb_rel_path")
    return {p for p in (decoded.get(k) for k in keys) if isinstance(p, str)}


def _file(rel_path: str) -> "Path | None":
    root = (persistent.state_file.parent / "attachments").resolve()
    path = (persistent.state_file.parent / rel_path).resolve()
    return path if path.is_relative_to(root) else None


def unlink(rel_paths: "set[str]") -> None:
    """Delete these attachment files, never one outside the state dir."""
    for rel_path in rel_paths:
        path = _file(rel_path)
        if path is None:
            logger.warning(
                "not deleting attachment outside the state dir: %r", rel_path
            )
            continue
        path.unlink(missing_ok=True)


async def remaining(
    sess: AsyncSession,
    conversation_id: int,
) -> "set[str]":
    """The attachment files the conversation's log still refers to."""
    payloads = (
        await sess.exec(
            select(persistent.ConversationLog.payload).where(
                col(persistent.ConversationLog.conversation_id)
                == conversation_id,
            )
        )
    ).all()
    return set().union(*(paths_in(p) for p in payloads))
