"""Pure decision layer for opportunistic ACKs, backfill, and resync.

No Qt, no SQLModel, no ThinClient, no asyncio: every state machine and
function here is plain data in, plain data out, so it is testable without a
session, a connection, or a docker mixnet. See "Opportunistic
acknowledgements and backfill" and "Optimistic resync" in the group chat
protocol spec.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TypeAlias

REPLICA_EPOCH_SECONDS = 7 * 24 * 3600
"""A replica epoch's length, mirrored from the replica source (see
``replica/state_gc.go``'s ``ReplicaEpochPeriod``). Not used by anything in
this module directly -- kept here as the one place that constant is named,
for tests and callers that need to reason about it in real time (e.g. the
Sent-box retention window)."""


class ScanState(enum.Enum):
    """States of the reader's scan state machine (see "Optimistic resync",
    item 2, in the group chat spec)."""

    READING = "reading"
    SCANNING = "scanning"


@dataclass(frozen=True)
class ReaderScan:
    """A reader's scan state for one monitored channel."""

    state: ScanState = ScanState.READING


# Events. There is no elapsed-time or "stall detected" event: a
# BoxIDNotFound means exactly "nothing has ever been written here", which
# is indistinguishable from an ordinarily quiet stream, forever -- no
# threshold on how long that has continued turns it into reliable
# detection (see "Optimistic resync" in the spec). So `ReadNotFound` in
# `READING` is never itself escalated by this function; only an explicit
# `ScanRequested`, sourced from the user asking their client to scan, does.

@dataclass(frozen=True)
class ReadOk:
    """A box was read and holds a real message."""

    payload: bytes


@dataclass(frozen=True)
class ReadTombstoned:
    """A box was read and holds a tombstone."""


@dataclass(frozen=True)
class ReadNotFound:
    """A box was probed and nothing has ever been written there (yet, or
    ever, or any longer -- this alone can never tell which)."""


@dataclass(frozen=True)
class ScanRequested:
    """The user asked their client to scan this channel."""


ScanEvent: TypeAlias = "ReadOk | ReadTombstoned | ReadNotFound | ScanRequested"


# Effects: instructions back to the driver. `step` never performs I/O or
# touches a database itself; it only says what the driver should do next.

@dataclass(frozen=True)
class Ingest:
    """Route `payload` through the normal dispatch path."""

    payload: bytes


@dataclass(frozen=True)
class AdvanceExpected:
    """The box at the expected position was accounted for; move on to the
    position that follows it."""


@dataclass(frozen=True)
class ProbeForward:
    """Derive and read the position that follows the one just probed."""


@dataclass(frozen=True)
class ProbeBackward:
    """Recheck the short trailing window of positions already passed,
    using index values retained from when they were read (see "Optimistic
    resync" in the spec: a BACAP index can only be advanced, never
    recovered backward)."""


@dataclass(frozen=True)
class AdoptFrontier:
    """The probed position is the true current end of the stream; make it
    the new expected position."""


ScanEffect: TypeAlias = (
    "Ingest | AdvanceExpected | ProbeForward | ProbeBackward | AdoptFrontier"
)


def step(scan: ReaderScan, event: ScanEvent) -> "tuple[ReaderScan, list[ScanEffect]]":
    """One transition of the reader's scan state machine. Total over every
    (state, event) pair: a combination the spec's transition table leaves
    out because nothing changes is a no-op here (same state, no effects),
    spelled out rather than left undefined.

    No clock: nothing here is time-dependent. `READING` retries the
    expected position exactly as it always has, indefinitely, on
    `ReadNotFound` -- that outcome never by itself starts or advances
    anything, because it cannot be told apart from an ordinarily quiet
    stream. The only way into `SCANNING` is `ScanRequested`.
    """
    if scan.state is ScanState.READING:
        if isinstance(event, ReadOk):
            return scan, [Ingest(event.payload), AdvanceExpected()]
        if isinstance(event, ReadTombstoned):
            return scan, [AdvanceExpected()]
        if isinstance(event, ReadNotFound):
            return scan, []
        if isinstance(event, ScanRequested):
            return ReaderScan(ScanState.SCANNING), [ProbeBackward(), ProbeForward()]
        raise TypeError(f"unhandled event {event!r} in {scan.state}")

    if scan.state is ScanState.SCANNING:
        if isinstance(event, ReadOk):
            return scan, [Ingest(event.payload), ProbeForward()]
        if isinstance(event, ReadTombstoned):
            return scan, [ProbeForward()]
        if isinstance(event, ReadNotFound):
            return ReaderScan(ScanState.READING), [AdoptFrontier()]
        if isinstance(event, ScanRequested):
            return scan, []
        raise TypeError(f"unhandled event {event!r} in {scan.state}")

    raise TypeError(f"unhandled state {scan.state!r}")  # pragma: no cover


@dataclass(frozen=True)
class SentBoxRecord:
    """Mirrors the relevant ``SentBox`` columns; nothing else."""

    box_index: bytes
    counter: int
    written_epoch: int
    payload: "bytes | None" = None


def select_backfill(
    journal: "list[SentBoxRecord]", current_epoch: int,
) -> "list[SentBoxRecord]":
    """Rows due for the periodic refresh's rewrite: whatever has not
    already been rewritten this replica epoch. This is the sweep's only
    eligibility check (Part 4); an ACK never narrows it (see "Backfill is
    not ACK-triggered" in the plan)."""
    return [row for row in journal if row.written_epoch < current_epoch]
