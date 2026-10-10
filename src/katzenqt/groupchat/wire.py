"""What a group chat message says to the protocol, and the hand-made
encodings of its control fields. See "Opportunistic acknowledgements and
backfill" in the group chat spec.
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


def key_of(read_cap: bytes) -> Key:
    return read_cap[:KEY_SIZE]


def position(index: bytes) -> int:
    """The position a ``MessageBoxIndex`` names: its little-endian counter.

    >>> position((7).to_bytes(8, "little") + bytes(96))
    7
    """
    return int.from_bytes(index[:8], "little")


@dataclass(frozen=True)
class IntroRef:
    """An ``Introduction``: its introducer's roster index in the sender's
    roster, and its position on the introducer's stream."""

    introducer: int
    position: int


@dataclass(frozen=True)
class Introduction:
    name: str
    read_cap: bytes


@dataclass(frozen=True)
class Message:
    """``content`` is opaque here. A message carries content or control,
    never both."""

    content: bytes | None = None
    introduction: Introduction | None = None
    numbers: tuple[tuple[int, IntroRef], ...] = ()
    introduced_at: IntroRef | None = None
    acks: Mapping[int, bytes] = field(default_factory=dict)

    @property
    def control(self) -> bool:
        return bool(
            self.introduction or self.numbers or self.introduced_at
        )


def encode_numbers(numbers: tuple[tuple[int, IntroRef], ...]) -> bytes:
    """
    >>> decode_numbers(encode_numbers(((3, IntroRef(1, 9)),)))
    ((3, IntroRef(introducer=1, position=9)),)
    """
    return b"".join(
        bytes([index, ref.introducer]) + ref.position.to_bytes(8, "little")
        for index, ref in numbers
    )


def decode_numbers(field: bytes) -> tuple[tuple[int, IntroRef], ...]:
    """Empty when the field is malformed.

    >>> decode_numbers(bytes(11)), decode_numbers(bytes(20))
    ((), ())
    """
    if len(field) % 10:
        return ()
    numbers = tuple(
        (
            field[i],
            IntroRef(field[i + 1], int.from_bytes(field[i + 2 : i + 10], "little")),
        )
        for i in range(0, len(field), 10)
    )
    indexes = [index for index, _ in numbers]
    if any(a >= b for a, b in zip(indexes, indexes[1:])):
        return ()
    return numbers


def encode_introduced_at(ref: IntroRef) -> bytes:
    return encode_numbers(((0, ref),))[1:]


def decode_introduced_at(field: bytes) -> IntroRef | None:
    """
    >>> decode_introduced_at(encode_introduced_at(IntroRef(2, 5)))
    IntroRef(introducer=2, position=5)
    >>> decode_introduced_at(b"short") is None
    True
    """
    if len(field) != 9:
        return None
    return decode_numbers(b"\0" + field)[0][1]


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
