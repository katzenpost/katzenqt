"""Thin wrappers over pycrdt's update mechanism, for catch-up over the chat.

A joiner or reconnector sends its state vector; a peer replies with the diff
since that vector; the joiner applies it and is caught up in one exchange,
regardless of how much it missed.

Note the argument convention of the installed pycrdt (0.13.x): ``get_update``
takes the remote state vector positionally.
"""
from __future__ import annotations

from pycrdt import Doc


def state_vector(doc: Doc) -> bytes:
    """The compact summary of what ``doc`` already has."""
    return doc.get_state()


def diff_since(doc: Doc, remote_state: bytes) -> bytes:
    """The update carrying everything ``doc`` has that ``remote_state`` lacks."""
    return doc.get_update(remote_state)


def full_state(doc: Doc) -> bytes:
    """The whole of ``doc`` as a single update (used to broadcast a new survey
    and to persist the survey's state as one replaceable blob)."""
    return doc.get_update()


def apply_update(doc: Doc, blob: bytes) -> None:
    """Merge a received update into ``doc``."""
    doc.apply_update(blob)


def load_doc(blob: bytes) -> Doc:
    """Rebuild a ``Doc`` from a :func:`full_state` blob. The root types are
    materialised by the update itself."""
    doc = Doc()
    doc.apply_update(blob)
    return doc
