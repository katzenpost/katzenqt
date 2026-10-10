"""Acknowledgements: what this member has published, what others have
acknowledged of its stream, and how far others have read every stream. An
induction holds publishing back. See "Joining" in the group chat spec.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .wire import Key, position


class Induction(enum.Enum):
    IDLE = "idle"
    CATCHING_UP = "catching up"
    INDUCTING = "inducting"


@dataclass
class Heard:
    index: bytes
    by: Key


@dataclass
class Ledger:
    published: dict[Key, bytes] = field(default_factory=dict)
    """The last index acknowledged of each stream."""
    acked_by: dict[Key, int] = field(default_factory=dict)
    """How far each member has acknowledged our stream, after the Sent-box
    check."""
    heard: dict[Key, Heard] = field(default_factory=dict)
    """The furthest box any member has acknowledged of each stream."""
    induction: Induction = Induction.IDLE
    invitee: tuple[str, bytes] | None = None
    """The name and read cap of the member being inducted."""
    commit: bool = False
    """Commit the induction as soon as catching up is done."""
    starts: dict[Key, bytes] = field(default_factory=dict)
    """Where each stream had been read when the invitation began."""

    @property
    def holding(self) -> bool:
        return self.induction is Induction.INDUCTING

    def owed(self, stream: Key, last: bytes | None) -> bytes | None:
        """The index to acknowledge of ``stream``, if anything new."""
        if self.holding or last is None or self.published.get(stream) == last:
            return None
        return last

    def publish(self, stream: Key, index: bytes) -> None:
        self.published[stream] = index

    def hear(self, stream: Key, index: bytes, by: Key) -> None:
        heard = self.heard.get(stream)
        if heard is None or position(index) > position(heard.index):
            self.heard[stream] = Heard(index, by)

    def acked(self, member: Key, at: int) -> None:
        self.acked_by[member] = max(self.acked_by.get(member, -1), at)
