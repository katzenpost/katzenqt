"""Colour each chat row by the group state it arrived in."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

MEMBERSHIP_SENTINELS = (b"TODO" * 8, bytes(32))
SUSPECT_COLOR = "#ff3b30"
NO_COLOR = ""


def is_real_hash(value: "bytes | None") -> bool:
    """Whether a membership hash is a hash and not a placeholder.

    >>> is_real_hash(bytes(range(32)))
    True
    >>> is_real_hash(None), is_real_hash(b"TODO" * 8), is_real_hash(bytes(32))
    (False, False, False)
    >>> is_real_hash(b"short")
    False
    """
    return (
        value is not None
        and len(value) == 32
        and value not in MEMBERSHIP_SENTINELS
    )


def color_for(value: "bytes | None") -> str:
    """The colour a row claiming this membership hash is tinted with.

    A row claiming nothing is left alone: there is nothing to show, and a
    colour would say more than we know. A row claiming something that is
    not a hash gets one reserved loud colour, so a run of them stands out
    instead of blending into the group before it.

    >>> color_for(None)
    ''
    >>> color_for(b"TODO" * 8)
    '#ff3b30'
    >>> color_for(bytes(range(32))) == color_for(bytes(range(32)))
    True
    >>> color_for(bytes(range(32))) != color_for(bytes(range(1, 33)))
    True
    """
    if value is None:
        return NO_COLOR
    if not is_real_hash(value):
        return SUSPECT_COLOR
    digest = hashlib.sha256(value).digest()
    return "#{:02x}{:02x}{:02x}".format(digest[0], digest[1], digest[2])


@dataclass(frozen=True)
class Row:
    color: str
    boundary: bool
    suspect: bool


def annotate(values: "Sequence[bytes | None]") -> "list[Row]":
    """Turn each row's claimed hash into a colour and a boundary flag.

    ``None`` is a row that claims nothing, such as one written before the
    protocol carried a hash, or a marker this client wrote for its own
    upload. It is left untinted and does not interrupt the run around it.
    A row claiming something that is not a hash keeps its own state
    rather than inheriting the one above, so entering and leaving such a
    run both read as a change.
    """
    rows: "list[Row]" = []
    previous: "bytes | None" = None
    seen = False
    for value in values:
        if value is None:
            rows.append(Row(color=NO_COLOR, boundary=False, suspect=False))
            continue
        real = is_real_hash(value)
        key = value if real else None
        rows.append(Row(
            color=color_for(value),
            boundary=seen and key != previous,
            suspect=not real,
        ))
        previous = key
        seen = True
    return rows
