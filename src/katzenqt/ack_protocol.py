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
this module directly -- callers derive their own threshold from it (or from
whatever epoch length applies) -- but kept here as the one place that
constant is named, for tests and callers alike."""


class ScanState(enum.Enum):
    """States of the reader's stall-detection-and-scan state machine (see
    "Optimistic resync", item 2, in the group chat spec)."""

    WAITING = "waiting"
    STALLED = "stalled"
    SCANNING = "scanning"


@dataclass(frozen=True)
class ReaderScan:
    """A reader's stall-detection-and-scan state for one monitored channel.

    ``stalled_since`` is ``None`` in :attr:`ScanState.WAITING`; it is set the
    moment a read of the expected next box first comes back not-found, and
    cleared whenever the state returns to :attr:`ScanState.WAITING`. It
    survives a restart unchanged (``ReadCapWAL.stalled_since``), so elapsed
    wall-clock time is measured correctly even across downtime.
    """

    state: ScanState = ScanState.WAITING
    stalled_since: "float | None" = None


# Events: the outcome of one read attempt. There is no separate "clock
# tick" event: the driver's read loop already retries the expected
# position on its own paced cadence (see `_pacer` in network.py), and every
# such retry is a real attempt that gets one of these three outcomes -- the
# real driver never has occasion to check the clock except when a retry
# has just come back not-found, so `step` checks it exactly then, as part
# of handling that outcome, rather than needing its own event type.

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
    ever)."""


ScanEvent: TypeAlias = "ReadOk | ReadTombstoned | ReadNotFound"


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


def step(
    scan: ReaderScan,
    event: ScanEvent,
    *,
    now: float,
    stall_threshold_s: float,
) -> "tuple[ReaderScan, list[ScanEffect]]":
    """One transition of the reader's stall-detection-and-scan state
    machine. Total over every (state, event) pair: a combination the spec's
    transition table leaves out because nothing changes is a no-op here
    (same state, no effects), spelled out rather than left undefined.

    `now` and `stall_threshold_s` are always supplied, whether or not this
    particular event needs them, so the signature never changes shape. The
    escalation check (has this gone on long enough to start scanning) is
    evaluated here, as part of handling a `ReadNotFound` while already
    `STALLED` -- not on some separate timer -- because that is genuinely
    the only moment the driver has fresh information to check it against:
    every `STALLED`-state retry is a real read attempt at the driver's own
    paced cadence, and a `ReadNotFound` outcome is what this function
    receives each time one of those comes back empty.
    """
    if scan.state is ScanState.WAITING:
        if isinstance(event, ReadOk):
            return ReaderScan(ScanState.WAITING), [Ingest(event.payload), AdvanceExpected()]
        if isinstance(event, ReadTombstoned):
            return ReaderScan(ScanState.WAITING), [AdvanceExpected()]
        if isinstance(event, ReadNotFound):
            return ReaderScan(ScanState.STALLED, stalled_since=now), []
        raise TypeError(f"unhandled event {event!r} in {scan.state}")

    if scan.state is ScanState.STALLED:
        if isinstance(event, ReadOk):
            return ReaderScan(ScanState.WAITING), [Ingest(event.payload), AdvanceExpected()]
        if isinstance(event, ReadTombstoned):
            return ReaderScan(ScanState.WAITING), [AdvanceExpected()]
        if isinstance(event, ReadNotFound):
            assert scan.stalled_since is not None
            if now - scan.stalled_since > stall_threshold_s:
                return (
                    ReaderScan(ScanState.SCANNING, stalled_since=scan.stalled_since),
                    [ProbeBackward(), ProbeForward()],
                )
            # Still stalled; stalled_since is the FIRST not-found and is
            # never reset by a later one.
            return scan, []
        raise TypeError(f"unhandled event {event!r} in {scan.state}")

    if scan.state is ScanState.SCANNING:
        if isinstance(event, ReadOk):
            return scan, [Ingest(event.payload), ProbeForward()]
        if isinstance(event, ReadTombstoned):
            return scan, [ProbeForward()]
        if isinstance(event, ReadNotFound):
            return ReaderScan(ScanState.WAITING), [AdoptFrontier()]
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
