"""Reading one member's stream: in order, a box at a time, with a short
window of kept indexes for rechecking, and a scan that can jump ahead on
another member's acknowledgement. See "Rewrite and scan" in the group chat
spec.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .events import DeriveIndexes, Effect, Missing, ReadBox
from .wire import Key, position


class Mode(enum.Enum):
    IDLE = "idle"
    READING = "reading"
    SCANNING = "scanning"


@dataclass
class Reader:
    stream: Key
    read_cap: bytes
    next: bytes
    """The index of the next box to read."""
    last: bytes | None = None
    """The last box read, data or tombstone: how far it may be acked."""
    kept: dict[int, bytes] = field(default_factory=dict)
    """Positions passed without data, by position, for rechecking."""
    mode: Mode = Mode.IDLE
    scanned_to: int = -1
    """The furthest acknowledged position a scan has already jumped to."""

    @property
    def position(self) -> int:
        return position(self.next)

    def read(self) -> list[Effect]:
        if self.mode is not Mode.IDLE:
            return []
        self.mode = Mode.READING
        return [ReadBox(self.stream, self.read_cap, self.next)]

    def scan(self, heard: bytes | None, window: int) -> list[Effect]:
        """Recheck what was kept, then jump to ``heard`` if it lies ahead,
        or probe forward from here."""
        self.mode = Mode.SCANNING
        effects: list[Effect] = [
            ReadBox(self.stream, self.read_cap, index, recheck=True)
            for _, index in sorted(self.kept.items())
        ]
        if heard is not None and position(heard) > self.position:
            target = position(heard)
            self.scanned_to = target
            skipped = target - self.position
            effects.append(
                DeriveIndexes(
                    "jump",
                    self.stream,
                    self.next,
                    max(0, skipped - window),
                    min(window, skipped),
                )
            )
            self.next = heard
            return effects
        return effects + [ReadBox(self.stream, self.read_cap, self.next)]

    def jumped(self, skipped: tuple[bytes, ...]) -> list[Effect]:
        for index in skipped:
            self.kept[position(index)] = index
        return [ReadBox(self.stream, self.read_cap, self.next)]

    def found(
        self, index: bytes, result: bytes | Missing, next_index: bytes | None, window: int
    ) -> tuple[bytes | None, list[Effect]]:
        """Take one box read. Returns its plaintext if it is new data to
        ingest, and what to read next."""
        at = position(index)
        if at in self.kept and self.kept[at] == index:
            if isinstance(result, bytes):
                del self.kept[at]
                return result, []
            return None, []
        if index != self.next or self.mode is Mode.IDLE:
            return None, []
        if result is Missing.NOT_FOUND or next_index is None:
            self.mode = Mode.IDLE
            return None, []
        if result is Missing.TOMBSTONE:
            self.kept[at] = index
        self.last = index
        self.next = next_index
        horizon = self.position - window
        self.kept = {p: i for p, i in self.kept.items() if p >= horizon}
        effects: list[Effect] = [ReadBox(self.stream, self.read_cap, self.next)]
        return (result if isinstance(result, bytes) else None), effects
