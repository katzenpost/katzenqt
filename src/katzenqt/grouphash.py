"""Colour each chat row by the group state it arrived in."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

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

    A row whose group state we could not work out is left alone: there is
    nothing to show, and a colour would say more than we know. A state we
    have but which is not a hash gets one reserved loud colour, so a run
    of them stands out instead of blending into the group before it.

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


def boundaries(values: "Sequence[bytes | None]") -> "list[bool]":
    """Where the run of equal values breaks, for a striped divider.

    The first row is never a boundary, and a value of ``None`` -- a row
    whose group state we could not work out -- neither starts nor ends a
    run, so an unknown row does not invent a change.

    >>> a, b = bytes(range(32)), bytes(range(1, 33))
    >>> boundaries([])
    []
    >>> boundaries([a, a, b, b, a])
    [False, False, True, False, True]
    >>> boundaries([a, None, a])
    [False, False, False]
    >>> boundaries([a, None, b])
    [False, False, True]
    """
    flags: "list[bool]" = []
    previous: "bytes | None" = None
    seen = False
    for value in values:
        if value is None:
            flags.append(False)
            continue
        flags.append(seen and value != previous)
        previous = value
        seen = True
    return flags
