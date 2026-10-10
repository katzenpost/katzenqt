"""The boxes a member wrote to its own stream: what it may still rewrite,
and what its retention policy lets it forget. See "Sent-box records" and
"Disappearing messages" in the group chat spec.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .events import Effect, WriteBox, WriteTombstone


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
    index: bytes
    plaintext: bytes | None
    control: bool
    written: int


@dataclass
class SentBox:
    records: dict[int, Record] = field(default_factory=dict)
    backfilled: int = -1

    def record(self, at: int, index: bytes, plaintext: bytes, control: bool, epoch: int) -> None:
        self.records[at] = Record(index, plaintext, control, epoch)

    def check(self, at: int, index: bytes) -> bool:
        """The Sent-box check: ``index`` names a box we wrote."""
        record = self.records.get(at)
        return record is not None and record.index == index

    def retain(
        self, epoch: int, policy: Retention, everyone_acked: Callable[[int], bool]
    ) -> None:
        for at in [at for at, r in self.records.items() if epoch - r.written > policy.window]:
            del self.records[at]
        for at, record in self.records.items():
            if (
                not record.control
                and policy.keep_sent is not None
                and epoch - record.written >= policy.keep_sent
            ) or (policy.discard_acked and everyone_acked(at)):
                record.plaintext = None

    def backfill(self, epoch: int) -> list[Effect]:
        """Rewrite every record, at most once per epoch."""
        if epoch == self.backfilled:
            return []
        self.backfilled = epoch
        return [
            WriteBox(r.index, r.plaintext) if r.plaintext is not None else WriteTombstone(r.index)
            for _, r in sorted(self.records.items())
        ]
