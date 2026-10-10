"""What a group chat message says to the protocol, and the hand-made
encodings of its control fields. See "Opportunistic acknowledgements and
backfill" in the group chat spec.

A box is named by its ``MessageBoxIndex``: an opaque BACAP value, compared
only for equality and moved forward only by BACAP.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

import cbor2

from .. import ack_codec

Key = bytes
"""A member's identity: the public key its read cap begins with."""

KEY_SIZE = 32
INDEX_SIZE = 104
ROSTER_MAX = 256
"""The most entries a roster holds: roster indexes are one byte."""

_ENTRY = 2 + INDEX_SIZE


def key_of(read_cap: bytes) -> Key:
    return read_cap[:KEY_SIZE]


@dataclass(frozen=True)
class IntroRef:
    """An ``Introduction``: its introducer's roster index in the sender's
    roster, and its ``MessageBoxIndex``."""

    introducer: int
    index: bytes


@dataclass(frozen=True)
class Introduction:
    name: str
    read_cap: bytes


Numbers = tuple[tuple[int, IntroRef | None], ...]
"""Roster indexes assigned, each with the ``Introduction`` of the member
assigned it; None for the ``Introduction`` the message itself carries."""


@dataclass(frozen=True)
class Message:
    """``content`` is opaque here. A message carries content or control,
    never both."""

    content: bytes | None = None
    introduction: Introduction | None = None
    numbers: Numbers = ()
    introduced_at: IntroRef | None = None
    acks: Mapping[int, bytes] = field(default_factory=dict)

    @property
    def control(self) -> bool:
        return bool(
            self.introduction or self.numbers or self.introduced_at
        )


def encode_numbers(numbers: Numbers) -> bytes:
    """
    >>> ref = IntroRef(1, bytes(104))
    >>> decode_numbers(encode_numbers(((3, ref),))) == ((3, ref),)
    True
    >>> encode_numbers(((4, None),))
    b'\\x04'
    """
    if len(numbers) == 1 and numbers[0][1] is None:
        return bytes([numbers[0][0]])
    return b"".join(
        bytes([index, ref.introducer]) + ref.index
        for index, ref in numbers
        if ref is not None
    )


def decode_numbers(field: bytes) -> Numbers:
    """Empty when the field is malformed.

    >>> decode_numbers(b"\\x04")
    ((4, None),)
    >>> decode_numbers(bytes(107)), decode_numbers(bytes(212))
    ((), ())
    """
    if len(field) == 1:
        return ((field[0], None),)
    if len(field) % _ENTRY:
        return ()
    numbers: Numbers = tuple(
        (field[i], IntroRef(field[i + 1], field[i + 2 : i + _ENTRY]))
        for i in range(0, len(field), _ENTRY)
    )
    indexes = [index for index, _ in numbers]
    if any(a >= b for a, b in zip(indexes, indexes[1:])):
        return ()
    return numbers


def encode_introduced_at(ref: IntroRef) -> bytes:
    return bytes([ref.introducer]) + ref.index


def decode_introduced_at(field: bytes) -> IntroRef | None:
    """
    >>> ref = IntroRef(2, bytes(104))
    >>> decode_introduced_at(encode_introduced_at(ref)) == ref
    True
    >>> decode_introduced_at(b"short") is None
    True
    """
    if len(field) != 1 + INDEX_SIZE:
        return None
    return IntroRef(field[0], field[1:])


class Codec(Protocol):
    def encode(self, message: Message) -> bytes: ...

    def decode(self, plaintext: bytes) -> Message | None: ...


class CborCodec:
    """A plain encoding of :class:`Message`; a client supplies its own."""

    def encode(self, message: Message) -> bytes:
        gcm: dict[str, object] = {"Version": 1}
        if message.content is not None:
            gcm["Content"] = message.content
        if message.introduction is not None:
            gcm["Introduction"] = {
                "DisplayName": message.introduction.name,
                "UniversalReadCap": message.introduction.read_cap,
            }
        if message.numbers:
            gcm["Numbers"] = encode_numbers(message.numbers)
        if message.introduced_at is not None:
            gcm["IntroducedAt"] = encode_introduced_at(message.introduced_at)
        if message.acks:
            gcm["Acks"] = ack_codec.encode(message.acks)
        return cbor2.dumps(gcm)

    def decode(self, plaintext: bytes) -> Message | None:
        """
        >>> codec = CborCodec()
        >>> codec.decode(codec.encode(Message(content=b"hi"))).content
        b'hi'
        >>> codec.decode(b"\\xff") is None
        True
        """
        try:
            gcm = cbor2.loads(plaintext)
            intro = gcm.get("Introduction")
            try:
                acks = ack_codec.decode(gcm.get("Acks") or b"")
            except ack_codec.AcksError:
                acks = {}
            return Message(
                content=gcm.get("Content"),
                introduction=(
                    Introduction(intro["DisplayName"], intro["UniversalReadCap"])
                    if intro
                    else None
                ),
                numbers=decode_numbers(gcm.get("Numbers") or b""),
                introduced_at=decode_introduced_at(gcm.get("IntroducedAt") or b""),
                acks=acks,
            )
        except (cbor2.CBORDecodeError, AttributeError, KeyError, TypeError):
            return None
