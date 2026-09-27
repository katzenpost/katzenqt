"""Data-driven and property-based tests for the pure ack_protocol module.

No session, no connection, no fake, no docker: everything here is a plain
function or state-machine step over plain dataclasses.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from katzenqt import ack_protocol as ap

# ---------------------------------------------------------------------------
# ReaderScan.step: one case per row of the group chat spec's transition
# table ("Optimistic resync", item 2), plus the no-op rows that table omits
# because nothing changes there. ack_protocol.step is total over every
# (state, event) pair; this table is the executable form of that claim.
# ---------------------------------------------------------------------------

_WAITING = ap.ReaderScan(ap.ScanState.WAITING)
_STALLED_AT_10 = ap.ReaderScan(ap.ScanState.STALLED, stalled_since=10.0)
_SCANNING_AT_10 = ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=10.0)

_PAYLOAD = b"a real message"

_CASES = [
    ("waiting/data ingests and advances",
     _WAITING, ap.ReadOk(_PAYLOAD), 0.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.Ingest(_PAYLOAD), ap.AdvanceExpected()]),
    ("waiting/tombstone advances, nothing to ingest",
     _WAITING, ap.ReadTombstoned(), 0.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.AdvanceExpected()]),
    ("waiting/not-found starts the stall clock",
     _WAITING, ap.ReadNotFound(), 5.0, 100.0,
     ap.ReaderScan(ap.ScanState.STALLED, stalled_since=5.0),
     []),
    ("waiting/elapsed is a no-op (not stalled)",
     _WAITING, ap.Elapsed(), 999.0, 100.0,
     _WAITING,
     []),
    ("stalled/data resolves it back to waiting",
     _STALLED_AT_10, ap.ReadOk(_PAYLOAD), 20.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.Ingest(_PAYLOAD), ap.AdvanceExpected()]),
    ("stalled/tombstone resolves it back to waiting",
     _STALLED_AT_10, ap.ReadTombstoned(), 20.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.AdvanceExpected()]),
    ("stalled/not-found again does not reset the clock",
     _STALLED_AT_10, ap.ReadNotFound(), 50.0, 100.0,
     _STALLED_AT_10,
     []),
    ("stalled/elapsed well before threshold is a no-op",
     _STALLED_AT_10, ap.Elapsed(), 50.0, 100.0,
     _STALLED_AT_10,
     []),
    ("stalled/elapsed exactly at threshold does not escalate (strict >)",
     _STALLED_AT_10, ap.Elapsed(), 110.0, 100.0,
     _STALLED_AT_10,
     []),
    ("stalled/elapsed past threshold escalates to scanning",
     _STALLED_AT_10, ap.Elapsed(), 111.0, 100.0,
     ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=10.0),
     [ap.ProbeBackward(), ap.ProbeForward()]),
    ("scanning/data ingests and keeps scanning",
     _SCANNING_AT_10, ap.ReadOk(_PAYLOAD), 200.0, 100.0,
     _SCANNING_AT_10,
     [ap.Ingest(_PAYLOAD), ap.ProbeForward()]),
    ("scanning/tombstone keeps scanning, nothing to ingest",
     _SCANNING_AT_10, ap.ReadTombstoned(), 200.0, 100.0,
     _SCANNING_AT_10,
     [ap.ProbeForward()]),
    ("scanning/not-found is the true frontier",
     _SCANNING_AT_10, ap.ReadNotFound(), 200.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.AdoptFrontier()]),
    ("scanning/elapsed is a no-op (escalation already happened)",
     _SCANNING_AT_10, ap.Elapsed(), 200.0, 100.0,
     _SCANNING_AT_10,
     []),
]


@pytest.mark.parametrize(
    "start, event, now, threshold, expected_state, expected_effects",
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_reader_scan_transition_table(
    start, event, now, threshold, expected_state, expected_effects,
):
    new_state, effects = ap.step(start, event, now=now, stall_threshold_s=threshold)
    assert new_state == expected_state
    assert effects == expected_effects


# ---------------------------------------------------------------------------
# Property-based tests: invariants that must hold across arbitrary inputs,
# including replica-epoch-scale durations, not just the hand-picked cases
# above.
# ---------------------------------------------------------------------------

_DURATION = st.floats(
    min_value=0, max_value=10 * ap.REPLICA_EPOCH_SECONDS,
    allow_nan=False, allow_infinity=False,
)
_TIMESTAMP = st.floats(
    min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False,
)


@settings(max_examples=200, deadline=None)
@given(
    stalled_since=_TIMESTAMP,
    delta=st.floats(
        min_value=-10, max_value=10 * ap.REPLICA_EPOCH_SECONDS,
        allow_nan=False, allow_infinity=False,
    ),
    threshold=_DURATION,
)
def test_stalled_elapsed_escalates_iff_strictly_past_threshold(
    stalled_since, delta, threshold,
):
    # `elapsed` is recomputed as `now - stalled_since`, exactly the
    # expression `step` itself evaluates, rather than compared against
    # `delta` directly: for a large `stalled_since` a tiny `delta` can be
    # lost to float rounding in `stalled_since + delta`, so `now -
    # stalled_since` is not always bit-identical to `delta` -- and it is
    # `step`'s own arithmetic this property means to check, not `delta`.
    scan = ap.ReaderScan(ap.ScanState.STALLED, stalled_since=stalled_since)
    now = stalled_since + delta
    elapsed = now - stalled_since
    new_scan, effects = ap.step(scan, ap.Elapsed(), now=now, stall_threshold_s=threshold)
    if elapsed > threshold:
        assert new_scan == ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=stalled_since)
        assert effects == [ap.ProbeBackward(), ap.ProbeForward()]
    else:
        assert new_scan == scan
        assert effects == []


@settings(max_examples=100, deadline=None)
@given(now=_TIMESTAMP, threshold=_DURATION)
def test_elapsed_is_a_noop_outside_stalled(now, threshold):
    for scan in (
        ap.ReaderScan(ap.ScanState.WAITING),
        ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=0.0),
    ):
        new_scan, effects = ap.step(scan, ap.Elapsed(), now=now, stall_threshold_s=threshold)
        assert new_scan == scan
        assert effects == []


@settings(max_examples=100, deadline=None)
@given(now=_TIMESTAMP, threshold=_DURATION)
def test_waiting_not_found_stamps_stall_start_at_now(now, threshold):
    new_scan, effects = ap.step(
        ap.ReaderScan(ap.ScanState.WAITING), ap.ReadNotFound(),
        now=now, stall_threshold_s=threshold,
    )
    assert new_scan == ap.ReaderScan(ap.ScanState.STALLED, stalled_since=now)
    assert effects == []


@settings(max_examples=100, deadline=None)
@given(stalled_since=_TIMESTAMP, now=_TIMESTAMP, threshold=_DURATION)
def test_stalled_not_found_never_resets_the_clock(stalled_since, now, threshold):
    scan = ap.ReaderScan(ap.ScanState.STALLED, stalled_since=stalled_since)
    new_scan, effects = ap.step(scan, ap.ReadNotFound(), now=now, stall_threshold_s=threshold)
    assert new_scan == scan
    assert effects == []


@settings(max_examples=100, deadline=None)
@given(stalled_since=_TIMESTAMP, now=_TIMESTAMP, threshold=_DURATION)
def test_data_or_tombstone_always_clears_the_stall(stalled_since, now, threshold):
    for scan, event, expected_effects in (
        (ap.ReaderScan(ap.ScanState.STALLED, stalled_since=stalled_since), ap.ReadOk(_PAYLOAD),
         [ap.Ingest(_PAYLOAD), ap.AdvanceExpected()]),
        (ap.ReaderScan(ap.ScanState.STALLED, stalled_since=stalled_since), ap.ReadTombstoned(),
         [ap.AdvanceExpected()]),
    ):
        new_scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
        assert new_scan == ap.ReaderScan(ap.ScanState.WAITING)
        assert effects == expected_effects


# ---------------------------------------------------------------------------
# select_backfill
# ---------------------------------------------------------------------------

def test_select_backfill_boundary_excludes_current_epoch():
    journal = [ap.SentBoxRecord(box_index=b"a", counter=1, written_epoch=5)]
    assert ap.select_backfill(journal, current_epoch=5) == []


def test_select_backfill_boundary_includes_previous_epoch():
    journal = [ap.SentBoxRecord(box_index=b"a", counter=1, written_epoch=4)]
    assert ap.select_backfill(journal, current_epoch=5) == journal


def test_select_backfill_empty_journal():
    assert ap.select_backfill([], current_epoch=5) == []


_journal_strategy = st.lists(
    st.builds(
        ap.SentBoxRecord,
        box_index=st.binary(min_size=1, max_size=4),
        counter=st.integers(min_value=0, max_value=10_000),
        written_epoch=st.integers(min_value=-10, max_value=10_000),
        payload=st.one_of(st.none(), st.binary(max_size=8)),
    ),
    max_size=20,
)


@settings(max_examples=100, deadline=None)
@given(journal=_journal_strategy, current_epoch=st.integers(min_value=-10, max_value=10_000))
def test_select_backfill_is_exactly_the_stale_epoch_subset(journal, current_epoch):
    result = ap.select_backfill(journal, current_epoch)
    assert result == [row for row in journal if row.written_epoch < current_epoch]
    assert all(row.written_epoch < current_epoch for row in result)
