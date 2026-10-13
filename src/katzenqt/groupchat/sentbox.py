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
    index: bytes
    """The box's whole ``MessageBoxIndex``."""
    plaintext: bytes | None
    """None once the plaintext is gone, or if it was never handed over."""
    control: bool
    written: int


@dataclass
class SentBox:
    records: dict[int, Record] = field(default_factory=dict)
    """By the box's ``Idx64``, which orders the boxes of our stream as we
    wrote them."""
    backfilled: int = -1

    def record(self, index: bytes, plaintext: bytes | None, control: bool, epoch: int) -> None:
        self.records[counter(index)] = Record(index, plaintext, control, epoch)

    def check(self, index: bytes) -> int | None:
        """The Sent-box check: the ``Idx64`` of the box ``index`` names, or
        None if we never wrote it."""
        record = self.records.get(counter(index))
        return counter(index) if record is not None and record.index == index else None

    def check_at(self, idx64: int) -> bool:
        """The Sent-box check for an acknowledgement, which names the box by
        its ``Idx64``."""
        return idx64 in self.records

    def retain(
        self, epoch: int, policy: Retention, everyone_acked: Callable[[int], bool]
    ) -> None:
        for idx64 in [i for i, r in self.records.items() if epoch - r.written > policy.window]:
            del self.records[idx64]
        for idx64, record in self.records.items():
            if (
                not record.control
                and policy.keep_sent is not None
                and epoch - record.written >= policy.keep_sent
            ) or (policy.discard_acked and everyone_acked(idx64)):
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
