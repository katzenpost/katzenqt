"""Reading one member's stream: in order, a box at a time, with a short
window of kept indexes for rechecking, and a scan that can jump ahead to a
box another member has acknowledged. See "Rewrite and scan" in the group
chat spec.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .events import Effect, Missing, ReadBox, Walk
from .wire import Key


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
    passed: int = 0
    """How many boxes this reader has passed: its own count, by which it
    measures how far behind it a kept box is."""
    kept: list[tuple[bytes, int]] = field(default_factory=list)
    """Boxes passed without data, oldest first, with the count they were
    passed at, for rechecking."""
    mode: Mode = Mode.IDLE
    tried: list[bytes] = field(default_factory=list)
    """Acknowledged boxes a scan has already looked for."""

    def read(self) -> list[Effect]:
        if self.mode is not Mode.IDLE:
            return []
        self.mode = Mode.READING
        return [ReadBox(self.stream, self.read_cap, self.next)]

    def ahead(self, heard: list[bytes]) -> list[bytes]:
        """Acknowledged boxes a scan has not looked for yet."""
        return [h for h in heard if h != self.next and h not in self.tried]

    def scan(self, heard: list[bytes], window: int) -> list[Effect]:
        """Recheck what was kept, then look for acknowledged boxes ahead,
        or probe forward from here."""
        self.mode = Mode.SCANNING
        effects: list[Effect] = [
            ReadBox(self.stream, self.read_cap, index, recheck=True) for index, _ in self.kept
        ]
        targets = self.ahead(heard)
        if targets:
            self.tried += targets
            return effects + [Walk(self.stream, self.next, tuple(targets), window)]
        return effects + [ReadBox(self.stream, self.read_cap, self.next)]

    def walked(
        self, target: bytes | None, skipped: tuple[bytes, ...], steps: int, window: int
    ) -> list[Effect]:
        if target is not None:
            start = self.passed + steps - len(skipped)
            self.kept += [(index, start + i) for i, index in enumerate(skipped)]
            self.passed += steps
            self.next = target
            self._forget(window)
        return [ReadBox(self.stream, self.read_cap, self.next)]

    def _forget(self, window: int) -> None:
        self.kept = [(index, at) for index, at in self.kept if at >= self.passed - window]

    def found(
        self, index: bytes, result: bytes | Missing, next_index: bytes | None, window: int
    ) -> tuple[bytes | None, list[Effect]]:
        """Take one box read. Returns its plaintext if it is new data to
        ingest, and what to read next."""
        kept = [entry for entry in self.kept if entry[0] == index]
        if kept:
            if isinstance(result, bytes):
                self.kept.remove(kept[0])
                return result, []
            return None, []
        if index != self.next or self.mode is Mode.IDLE:
            return None, []
        if result is Missing.NOT_FOUND or next_index is None:
            self.mode = Mode.IDLE
            return None, []
        if result is Missing.TOMBSTONE:
            self.kept.append((index, self.passed))
        self.last = index
        self.next = next_index
        self.passed += 1
        self._forget(window)
        effects: list[Effect] = [ReadBox(self.stream, self.read_cap, self.next)]
        return (result if isinstance(result, bytes) else None), effects
