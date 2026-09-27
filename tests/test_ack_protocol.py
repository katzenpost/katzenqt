"""Data-driven and property-based tests for the pure ack_protocol module.

No session, no connection, no fake, no docker: everything here is a plain
function or state-machine step over plain dataclasses.
"""
from __future__ import annotations

import pytest
from hypothesis import given, note, settings
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
    ("stalled/data resolves it back to waiting",
     _STALLED_AT_10, ap.ReadOk(_PAYLOAD), 20.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.Ingest(_PAYLOAD), ap.AdvanceExpected()]),
    ("stalled/tombstone resolves it back to waiting",
     _STALLED_AT_10, ap.ReadTombstoned(), 20.0, 100.0,
     ap.ReaderScan(ap.ScanState.WAITING),
     [ap.AdvanceExpected()]),
    ("stalled/not-found well before threshold is a no-op",
     _STALLED_AT_10, ap.ReadNotFound(), 60.0, 100.0,
     _STALLED_AT_10,
     []),
    ("stalled/not-found exactly at threshold does not escalate (strict >)",
     _STALLED_AT_10, ap.ReadNotFound(), 110.0, 100.0,
     _STALLED_AT_10,
     []),
    ("stalled/not-found past threshold escalates to scanning",
     _STALLED_AT_10, ap.ReadNotFound(), 111.0, 100.0,
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
def test_stalled_not_found_escalates_iff_strictly_past_threshold(
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
    new_scan, effects = ap.step(scan, ap.ReadNotFound(), now=now, stall_threshold_s=threshold)
    if elapsed > threshold:
        assert new_scan == ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=stalled_since)
        assert effects == [ap.ProbeBackward(), ap.ProbeForward()]
    else:
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


@settings(max_examples=100, deadline=None)
@given(stalled_since=_TIMESTAMP, now=_TIMESTAMP, threshold=_DURATION)
def test_scanning_never_reintroduces_a_stall(stalled_since, now, threshold):
    """Scanning always resolves straight to a fresh (unstalled) WAITING;
    the stall clock cannot leak forward into the next expected position."""
    for event, expected_effects in (
        (ap.ReadNotFound(), [ap.AdoptFrontier()]),
    ):
        scan = ap.ReaderScan(ap.ScanState.SCANNING, stalled_since=stalled_since)
        new_scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
        assert new_scan == ap.ReaderScan(ap.ScanState.WAITING)
        assert new_scan.stalled_since is None
        assert effects == expected_effects


# ---------------------------------------------------------------------------
# Stateful/invariant hunting: arbitrary sequences of events (not just one
# step), checking a structural invariant after every single step, rather
# than only the specific scenarios above.
# ---------------------------------------------------------------------------

_EVENTS = st.one_of(
    st.builds(ap.ReadOk, payload=st.binary(max_size=8)),
    st.builds(ap.ReadTombstoned),
    st.builds(ap.ReadNotFound),
)


@settings(max_examples=300, deadline=None)
@given(
    steps=st.lists(
        st.tuples(_EVENTS, _DURATION),
        min_size=1, max_size=50,
    ),
    threshold=_DURATION,
)
def test_stalled_since_is_none_iff_waiting(steps, threshold):
    """Across an arbitrary sequence of events (with a monotonic clock),
    `stalled_since is None` holds exactly when `state is WAITING`, in
    every state `step` ever returns -- never a WAITING with a leftover
    timestamp, never a STALLED/SCANNING with none."""
    scan = ap.ReaderScan(ap.ScanState.WAITING)
    now = 0.0
    for event, dt in steps:
        now += dt  # clock never goes backward
        scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
        note(f"now={now} event={event!r} -> {scan!r} {effects!r}")
        assert (scan.stalled_since is None) == (scan.state is ap.ScanState.WAITING)
        assert scan.state in ap.ScanState


@settings(max_examples=300, deadline=None)
@given(
    steps=st.lists(
        st.tuples(_EVENTS, _DURATION),
        min_size=1, max_size=50,
    ),
    threshold=_DURATION,
)
def test_ingest_effects_only_accompany_read_ok(steps, threshold):
    """An `Ingest` effect only ever appears when the triggering event was
    `ReadOk`, and always carries that exact payload -- `step` never
    invents or drops a payload."""
    scan = ap.ReaderScan(ap.ScanState.WAITING)
    now = 0.0
    for event, dt in steps:
        now += dt
        scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
        ingests = [e for e in effects if isinstance(e, ap.Ingest)]
        if isinstance(event, ap.ReadOk):
            assert ingests in ([], [ap.Ingest(event.payload)])
        else:
            assert ingests == []


@settings(max_examples=300, deadline=None)
@given(
    steps=st.lists(
        st.tuples(_EVENTS, _DURATION),
        min_size=1, max_size=50,
    ),
    threshold=_DURATION,
)
def test_step_is_deterministic(steps, threshold):
    """Replaying the exact same sequence of (event, now) pairs from the
    same starting state always reaches the same final state: `step` has no
    hidden state and no randomness."""
    def run():
        scan = ap.ReaderScan(ap.ScanState.WAITING)
        now = 0.0
        history = []
        for event, dt in steps:
            now += dt
            scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
            history.append((scan, tuple(effects)))
        return history

    assert run() == run()


# A clock that can also go backward (clock skew), to see whether `step`
# still behaves sanely -- no crash, and the same structural invariant
# holds -- even though the "correctly escalates" semantic guarantee is not
# expected to survive a clock running backward.
@settings(max_examples=300, deadline=None)
@given(
    steps=st.lists(
        st.tuples(_EVENTS, st.floats(
            min_value=-10 * ap.REPLICA_EPOCH_SECONDS,
            max_value=10 * ap.REPLICA_EPOCH_SECONDS,
            allow_nan=False, allow_infinity=False,
        )),
        min_size=1, max_size=50,
    ),
    threshold=_DURATION,
)
def test_no_crash_and_invariant_holds_even_under_clock_skew(steps, threshold):
    scan = ap.ReaderScan(ap.ScanState.WAITING)
    now = 0.0
    for event, dt in steps:
        now += dt
        scan, effects = ap.step(scan, event, now=now, stall_threshold_s=threshold)
        assert (scan.stalled_since is None) == (scan.state is ap.ScanState.WAITING)


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


@settings(max_examples=100, deadline=None)
@given(
    journal=_journal_strategy,
    epochs=st.lists(st.integers(min_value=-10, max_value=10_000), min_size=2, max_size=2),
)
def test_select_backfill_is_monotonic_in_current_epoch(journal, epochs):
    """A row eligible at some epoch is still eligible at any later epoch:
    increasing `current_epoch` can only add rows to the result, never drop
    one, since `written_epoch < current_epoch` only becomes easier to
    satisfy as `current_epoch` grows."""
    lo, hi = sorted(epochs)
    result_lo = ap.select_backfill(journal, lo)
    result_hi = ap.select_backfill(journal, hi)
    assert all(row in result_hi for row in result_lo)


@settings(max_examples=50, deadline=None)
@given(
    journal=_journal_strategy,
    current_epoch=st.one_of(
        st.integers(min_value=-10, max_value=10_000),
        st.just(0),
        st.just(-(2**63)),
        st.just(2**63 - 1),
    ),
)
def test_select_backfill_handles_extreme_epoch_ids(journal, current_epoch):
    """Nothing about this predicate depends on epoch numbers being small
    or positive -- it should behave the same at the extremes of a 64-bit
    range as anywhere else."""
    result = ap.select_backfill(journal, current_epoch)
    assert result == [row for row in journal if row.written_epoch < current_epoch]


# ---------------------------------------------------------------------------
# Joint simulation: Alice authors a channel, subject to simplified replica
# GC and her own periodic sweep; Bob reads it with a real ReaderScan. This
# is the closest a pure-Python property test gets to the cross-member
# liveness claim in "Optimistic resync": given weak fairness (each side
# comes back online at least once per replica epoch), does Bob always
# eventually reach Alice's true frontier, no matter how long he started out
# stalled behind a since-garbage-collected gap?
#
# This is deliberately a small abstraction, not a rebuild of the real GC or
# sweep code: a position "exists" if it was written or swept within the
# current or immediately preceding epoch (mirroring WipeStaleBoxes), and
# Alice's sweep refreshes every position she is online to run it against.
# Bob uses the real ap.step for his own side.
# ---------------------------------------------------------------------------

def _run_joint_simulation(
    n_positions, epoch_length, total_ticks, alice_online, bob_online, stall_threshold_s,
):
    # Alice wrote everything long before the simulation starts, and it has
    # already gone stale by tick 0 -- the interesting case is recovering
    # from a gap that already exists, not merely keeping up with new
    # writes (which ordinary reading, unmodified, already handles).
    last_refresh_epoch = [-1_000_000] * n_positions

    def epoch_of(tick):
        return tick // epoch_length

    def exists(i, tick):
        return epoch_of(tick) - last_refresh_epoch[i] <= 1

    scan = ap.ReaderScan(ap.ScanState.WAITING)
    next_index = 0
    scan_cursor = 0
    received: "set[int]" = set()  # positions genuinely ingested via ReadOk

    for tick in range(total_ticks):
        e = epoch_of(tick)
        if alice_online[tick]:
            for i in range(n_positions):
                if last_refresh_epoch[i] < e:
                    last_refresh_epoch[i] = e

        if not bob_online[tick]:
            continue

        # One ordinary read attempt per online tick while Waiting/Stalled.
        # Once actively Scanning, race through the backlog within this
        # same tick: deriving and probing the next index needs no network
        # round trip (see "Optimistic resync"), so it is not paced the way
        # an ordinary ReadNotFound retry is.
        while True:
            probe_index = next_index if scan.state in (
                ap.ScanState.WAITING, ap.ScanState.STALLED,
            ) else scan_cursor

            if probe_index >= n_positions:
                event = ap.ReadNotFound()
            elif exists(probe_index, tick):
                event = ap.ReadOk(str(probe_index).encode())
            else:
                event = ap.ReadNotFound()

            was_scanning = scan.state is ap.ScanState.SCANNING
            scan, effects = ap.step(
                scan, event, now=float(tick), stall_threshold_s=stall_threshold_s,
            )
            for effect in effects:
                if isinstance(effect, ap.Ingest):
                    received.add(int(effect.payload.decode()))
                elif isinstance(effect, ap.AdvanceExpected):
                    next_index += 1
                elif isinstance(effect, ap.AdoptFrontier):
                    next_index = probe_index
                    scan_cursor = probe_index
                elif isinstance(effect, ap.ProbeForward):
                    scan_cursor = probe_index + 1

            if not (was_scanning and scan.state is ap.ScanState.SCANNING):
                break

    return scan, next_index, received


def _weakly_fair_schedule(data, total_ticks, epoch_length, total_epochs):
    """One online tick per epoch, guaranteed, plus whatever extra ticks
    Hypothesis wants to add."""
    schedule = data.draw(st.lists(
        st.booleans(), min_size=total_ticks, max_size=total_ticks,
    ))
    for e in range(total_epochs):
        forced = data.draw(st.integers(min_value=0, max_value=epoch_length - 1))
        schedule[e * epoch_length + forced] = True
    return schedule


@settings(max_examples=300, deadline=None)
@given(
    n_positions=st.integers(min_value=1, max_value=12),
    epoch_length=st.integers(min_value=1, max_value=8),
    data=st.data(),
)
def test_mutual_resync_eventually_completes_given_weak_fairness(
    n_positions, epoch_length, data,
):
    # Ordinary reading (no stall ever triggered) advances at most one
    # position per Bob-ONLINE tick, and weak fairness only guarantees one
    # online tick per EPOCH -- so a reader who happens to succeed every
    # time it checks, but only checks once an epoch, genuinely progresses
    # at only one position per epoch: Scanning's same-tick backlog race
    # never engages, because nothing ever came back not-found for it to
    # react to. (An earlier version of this margin scaled by
    # n_positions/epoch_length, assuming once-per-TICK throughput, and
    # Hypothesis found the counterexample: n_positions=11, epoch_length=3,
    # Bob online once an epoch, never stalling, capped at one position per
    # epoch.) So the needed floor is n_positions epochs, not ticks, plus
    # slack for the stall-detection/escalation machinery on top of that.
    slack_epochs = data.draw(st.integers(min_value=6, max_value=20))
    total_epochs = n_positions + slack_epochs
    total_ticks = total_epochs * epoch_length
    stall_threshold_s = float(epoch_length)

    alice_online = _weakly_fair_schedule(data, total_ticks, epoch_length, total_epochs)
    bob_online = _weakly_fair_schedule(data, total_ticks, epoch_length, total_epochs)

    scan, next_index, received = _run_joint_simulation(
        n_positions, epoch_length, total_ticks,
        alice_online, bob_online, stall_threshold_s,
    )
    note(f"final scan={scan!r} next_index={next_index} received={received}")
    # Not just "the index counter reached n_positions" (which a reader can
    # satisfy by giving up on a permanently-lost position and adopting
    # whatever it finds next, per "advances its own expected position past
    # the stalled one" -- see the no-fairness test below for exactly that
    # happening): every position Alice ever wrote was genuinely delivered.
    assert received == set(range(n_positions)), (
        "Bob did not genuinely receive every position Alice wrote, even "
        "though both sides were online at least once per epoch throughout"
    )


@settings(max_examples=100, deadline=None)
@given(
    n_positions=st.integers(min_value=1, max_value=12),
    epoch_length=st.integers(min_value=1, max_value=8),
    data=st.data(),
)
def test_resync_is_not_vacuously_satisfied_without_fairness(n_positions, epoch_length, data):
    """The converse of the property above, hunted rather than assumed: with
    Alice never online at all (fairness violated on her side), Bob's scan
    still runs to completion and his own index bookkeeping still advances
    -- "advances past the stalled one" is exactly what it is designed to
    do, and it does not know position 0 was ever supposed to hold
    anything. What must NOT happen is content silently counting as
    delivered when it never was: `received` stays empty. This is what
    makes the property above non-vacuous -- the mechanism does not
    unconditionally launder a genuine loss into an apparent success."""
    total_epochs = data.draw(st.integers(min_value=6, max_value=14))
    total_ticks = total_epochs * epoch_length
    stall_threshold_s = float(epoch_length)

    # Alice is online nowhere at all: nothing is ever refreshed, so nothing
    # ever exists for Bob to find.
    alice_online = [False] * total_ticks
    bob_online = _weakly_fair_schedule(data, total_ticks, epoch_length, total_epochs)

    scan, next_index, received = _run_joint_simulation(
        n_positions, epoch_length, total_ticks,
        alice_online, bob_online, stall_threshold_s,
    )
    note(f"final scan={scan!r} next_index={next_index} received={received}")
    assert received == set()
