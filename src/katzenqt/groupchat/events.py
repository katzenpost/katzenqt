"""What a driver tells the group state machine, and what the machine asks
the driver to do. Effects map onto thin client calls: the machine never
derives a BACAP index, encrypts, or touches the network itself.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TypeAlias

from .wire import Key


class Missing(enum.Enum):
    TOMBSTONE = "tombstone"
    NOT_FOUND = "not found"


# Events.


@dataclass(frozen=True)
class Send:
    content: bytes


@dataclass(frozen=True)
class Read:
    stream: Key


@dataclass(frozen=True)
class Scan:
    stream: Key


@dataclass(frozen=True)
class BoxRead:
    """The result of :class:`ReadBox`. ``next_index`` is the index after
    ``index``, which a thin client read returns alongside the box."""

    stream: Key
    index: bytes
    result: bytes | Missing
    next_index: bytes | None = None


@dataclass(frozen=True)
class IndexesDerived:
    """The result of :class:`DeriveIndexes`."""

    purpose: str
    stream: Key
    indexes: tuple[bytes, ...]


@dataclass(frozen=True)
class Invite:
    """An induction begins: catch up, then hold acknowledgements back."""

    name: str
    read_cap: bytes


@dataclass(frozen=True)
class Introduce:
    """Commit an induction, starting one first if none is under way."""

    name: str
    read_cap: bytes


@dataclass(frozen=True)
class Remove:
    member: Key


@dataclass(frozen=True)
class EpochChanged:
    epoch: int


Event: TypeAlias = (
    Send | Read | Scan | BoxRead | IndexesDerived | Invite | Introduce
    | Remove | EpochChanged
)


# Effects.


@dataclass(frozen=True)
class WriteBox:
    index: bytes
    plaintext: bytes


@dataclass(frozen=True)
class WriteTombstone:
    index: bytes


@dataclass(frozen=True)
class ReadBox:
    stream: Key
    read_cap: bytes
    index: bytes
    recheck: bool = False


@dataclass(frozen=True)
class DeriveIndexes:
    """``count`` indexes of ``read_cap``'s stream, starting ``skip`` boxes
    after ``start``."""

    purpose: str
    stream: Key
    start: bytes
    skip: int
    count: int


@dataclass(frozen=True)
class Deliver:
    """Content read from ``sender``'s stream, for the user."""

    sender: Key
    position: int
    content: bytes


@dataclass(frozen=True)
class Expire:
    """The retention policy deletes content previously delivered."""

    sender: Key
    position: int


@dataclass(frozen=True)
class SendJoinReply:
    to: bytes
    reply: "JoinReply"


Effect: TypeAlias = (
    WriteBox | WriteTombstone | ReadBox | DeriveIndexes | Deliver | Expire
    | SendJoinReply
)


@dataclass(frozen=True)
class JoinReply:
    """What an introducer hands a new member. Members are named by their
    place in ``members``, which is the introducer's roster.

    ``members``: each member's read cap and start index, or None for a slot
    the introducer cannot or will not fill. ``rosters``: each member's
    roster as the introducer knows it, as (index, place, position the
    member numbered it at, or -1 if it started with it); None when unknown.
    ``introductions``: (introducer place, position, member place).
    """

    members: tuple[tuple[bytes, bytes] | None, ...]
    rosters: tuple[tuple[tuple[int, int | None, int], ...] | None, ...]
    introductions: tuple[tuple[int, int, int], ...]
    introducer: int
    """The introducer's place."""
    introduction: int
    """The position of the ``Introduction`` on the introducer's stream."""
