"""The ``Acks`` field of a group chat message: who is acknowledged, then
one value for each.

Pure: bytes in, plain data out; no session, no connection. See "Rosters"
(Layout, Parsing) in the group chat protocol spec. A member is named by its
roster index in the sender's own roster. The names are a list of roster
indexes or a bitmap, whichever is shorter, and the values follow back to
back in ascending order of roster index.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

VALUE_SIZE = 104
"""Bytes in one value: a BACAP ``MessageBoxIndex``."""

ROSTER_MAX = 256
"""The most entries a roster holds: roster indexes 0 to 255."""

MAX_NAMING = (ROSTER_MAX + 7) // 8
"""The longest list or bitmap, in bytes."""

MIN_VALUE_SIZE = MAX_NAMING + 1
"""A reader splits the field by dividing its length by the value size, so a
value must be longer than the longest list or bitmap."""


class AcksError(ValueError):
    """The field is malformed and carries no acknowledgements."""


def name_members(indexes: Sequence[int]) -> bytes:
    """Ascending roster indexes as a list or a bitmap, whichever is shorter.

    >>> name_members([9]).hex()
    '09'
    >>> name_members([3, 12]).hex()
    '030c'
    >>> name_members([3, 12, 13]).hex()
    '100c'
    >>> name_members(range(1, 16)).hex()
    '7fff'
    >>> name_members([])
    b''
    """
    if not indexes:
        return b""
    width = indexes[-1] // 8 + 1
    if len(indexes) <= width:
        return bytes(indexes)
    bitmap = bytearray(width)
    for index in indexes:
        bitmap[index // 8] |= 0x80 >> (index % 8)
    return bytes(bitmap)


def read_members(naming: bytes, count: int) -> list[int]:
    """What precedes ``count`` values: a list when it is ``count`` bytes
    long, a bitmap when it is shorter.

    >>> read_members(bytes.fromhex("030c"), 2)
    [3, 12]
    >>> read_members(bytes.fromhex("100c"), 3)
    [3, 12, 13]
    >>> read_members(bytes.fromhex("0503"), 2)
    Traceback (most recent call last):
        ...
    katzenqt.ack_codec.AcksError: list is not in ascending order
    """
    if len(naming) == count:
        indexes = list(naming)
        if any(a >= b for a, b in zip(indexes, indexes[1:])):
            raise AcksError("list is not in ascending order")
        return indexes
    if len(naming) > count:
        raise AcksError("more naming bytes than values")
    if len(naming) > MAX_NAMING:
        raise AcksError("bitmap is longer than a roster")
    if not naming or naming[-1] == 0:
        raise AcksError("bitmap ends in a zero byte")
    indexes = [
        index
        for index in range(len(naming) * 8)
        if naming[index // 8] & (0x80 >> (index % 8))
    ]
    if len(indexes) != count:
        raise AcksError("bitmap does not name one member per value")
    return indexes


def encode(acks: Mapping[int, bytes], value_size: int = VALUE_SIZE) -> bytes:
    """The field for ``acks``, a mapping of roster index to value. Empty
    when there is nothing to acknowledge.

    >>> field = encode({12: bytes(104), 3: bytes([1]) * 104})
    >>> field[:2].hex(), len(field)
    ('030c', 210)
    >>> field[2:106] == bytes([1]) * 104
    True
    >>> encode({})
    b''
    """
    if value_size < MIN_VALUE_SIZE:
        raise AcksError("value too short for the list-or-bitmap layout")
    indexes = sorted(acks)
    if indexes and not 0 <= indexes[0] <= indexes[-1] < ROSTER_MAX:
        raise AcksError("roster index out of range")
    if any(len(acks[index]) != value_size for index in indexes):
        raise AcksError("value of the wrong size")
    return name_members(indexes) + b"".join(acks[index] for index in indexes)


def decode(field: bytes, value_size: int = VALUE_SIZE) -> dict[int, bytes]:
    """Roster index to value, in ascending order of roster index. Raises
    :class:`AcksError` when the field is malformed; the caller then treats
    the message as carrying no acknowledgements.

    >>> decode(encode({9: bytes(104)})) == {9: bytes(104)}
    True
    >>> decode(b"")
    {}
    >>> decode(bytes([1, 2, 3]) + bytes(104))
    Traceback (most recent call last):
        ...
    katzenqt.ack_codec.AcksError: more naming bytes than values
    """
    if value_size < MIN_VALUE_SIZE:
        raise AcksError("value too short for the list-or-bitmap layout")
    count, width = divmod(len(field), value_size)
    indexes = read_members(field[:width], count)
    values = field[width:]
    return {
        index: values[i * value_size : (i + 1) * value_size]
        for i, index in enumerate(indexes)
    }
