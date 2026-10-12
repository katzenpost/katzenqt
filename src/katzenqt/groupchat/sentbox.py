"""The boxes a member wrote to its own stream: what it may still rewrite,
and what its retention policy lets it forget. See "Sent-box records" and
"Disappearing messages" in the group chat spec.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .events import Effect, WriteBox, WriteTombstone
from .wire import counter


@dataclass(frozen=True)
class Retention:
    window: int = 3
    """Epochs a Sent-box record is kept and backfilled."""
    discard_acked: bool = True
    """Drop plaintext every member still read has acknowledged."""
    keep_sent: int | None = None
    """Epochs the policy keeps sent content."""
    keep_received: int | None = None
    """Epochs the policy keeps received content."""


@dataclass
class Record:
    seq: int
    """Where the box falls in the order we wrote our boxes."""
    plaintext: bytes | None
    """None once the plaintext is gone, or if it was never handed over."""
    control: bool
    written: int


@dataclass
class SentBox:
    records: dict[bytes, Record] = field(default_factory=dict)
    """By ``MessageBoxIndex``."""
    count: int = 0
    backfilled: int = -1

    def record(self, index: bytes, plaintext: bytes | None, control: bool, epoch: int) -> None:
        self.records[index] = Record(self.count, plaintext, control, epoch)
        self.count += 1

    def check(self, index: bytes) -> int | None:
        """The Sent-box check: where in our order the box ``index`` names
        falls, or None if we never wrote it."""
        record = self.records.get(index)
        return None if record is None else record.seq

    def check_at(self, idx64: int) -> int | None:
        """The Sent-box check for an acknowledgement, which names the box by
        its ``Idx64``."""
        for index, record in self.records.items():
            if counter(index) == idx64:
                return record.seq
        return None

    def retain(
        self, epoch: int, policy: Retention, everyone_acked: Callable[[int], bool]
    ) -> None:
        for index in [i for i, r in self.records.items() if epoch - r.written > policy.window]:
            del self.records[index]
        for record in self.records.values():
            if (
                not record.control
                and policy.keep_sent is not None
                and epoch - record.written >= policy.keep_sent
            ) or (policy.discard_acked and everyone_acked(record.seq)):
                record.plaintext = None

    def backfill(self, epoch: int) -> list[Effect]:
        """Rewrite every record, at most once per epoch."""
        if epoch == self.backfilled:
            return []
        self.backfilled = epoch
        return [
            WriteBox(index, r.plaintext) if r.plaintext is not None else WriteTombstone(index)
            for index, r in sorted(self.records.items(), key=lambda item: item[1].seq)
        ]
