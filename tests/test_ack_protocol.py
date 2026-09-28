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
# table ("Optimistic resync", item 2). ack_protocol.step is total over every
# (state, event) pair (2 states x 4 events); this table is the executable
# form of that claim.
# ---------------------------------------------------------------------------

_READING = ap.ReaderScan(ap.ScanState.READING)
_SCANNING = ap.ReaderScan(ap.ScanState.SCANNING)

_PAYLOAD = b"a real message"

_CASES = [
    ("reading/data ingests and advances",
     _READING, ap.ReadOk(_PAYLOAD),
     _READING, [ap.Ingest(_PAYLOAD), ap.AdvanceExpected()]),
    ("reading/tombstone advances, nothing to ingest",
     _READING, ap.ReadTombstoned(),
     _READING, [ap.AdvanceExpected()]),
    ("reading/not-found is a no-op: indistinguishable from an ordinarily quiet stream",
     _READING, ap.ReadNotFound(),
     _READING, []),
    ("reading/a scan request starts scanning",
     _READING, ap.ScanRequested(),
     _SCANNING, [ap.ProbeBackward(), ap.ProbeForward()]),
    ("scanning/data ingests and keeps scanning",
     _SCANNING, ap.ReadOk(_PAYLOAD),
     _SCANNING, [ap.Ingest(_PAYLOAD), ap.ProbeForward()]),
    ("scanning/tombstone keeps scanning, nothing to ingest",
     _SCANNING, ap.ReadTombstoned(),
     _SCANNING, [ap.ProbeForward()]),
    ("scanning/not-found is the true frontier",
     _SCANNING, ap.ReadNotFound(),
     _READING, [ap.AdoptFrontier()]),
    ("scanning/a repeated scan request is a no-op: already scanning",
     _SCANNING, ap.ScanRequested(),
     _SCANNING, []),
]


@pytest.mark.parametrize(
    "start, event, expected_state, expected_effects",
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_reader_scan_transition_table(start, event, expected_state, expected_effects):
    new_state, effects = ap.step(start, event)
    assert new_state == expected_state
    assert effects == expected_effects


# ---------------------------------------------------------------------------
# Stateful/invariant hunting: arbitrary sequences of events, checking a
# structural invariant after every single step, rather than only the
# specific scenarios above.
# ---------------------------------------------------------------------------

_EVENTS = st.one_of(
    st.builds(ap.ReadOk, payload=st.binary(max_size=8)),
    st.builds(ap.ReadTombstoned),
    st.builds(ap.ReadNotFound),
    st.builds(ap.ScanRequested),
)


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_state_is_always_one_of_the_two(steps):
    scan = ap.ReaderScan(ap.ScanState.READING)
    for event in steps:
        scan, effects = ap.step(scan, event)
        note(f"event={event!r} -> {scan!r} {effects!r}")
        assert scan.state in ap.ScanState


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_ingest_effects_only_accompany_read_ok(steps):
    """An `Ingest` effect only ever appears when the triggering event was
    `ReadOk`, and always carries that exact payload -- `step` never invents
    or drops a payload."""
    scan = ap.ReaderScan(ap.ScanState.READING)
    for event in steps:
        scan, effects = ap.step(scan, event)
        ingests = [e for e in effects if isinstance(e, ap.Ingest)]
        if isinstance(event, ap.ReadOk):
            assert ingests in ([], [ap.Ingest(event.payload)])
        else:
            assert ingests == []


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_step_is_deterministic(steps):
    """Replaying the exact same sequence of events from the same starting
    state always reaches the same final state: `step` has no hidden state,
    no clock, and no randomness."""
    def run():
        scan = ap.ReaderScan(ap.ScanState.READING)
        history = []
        for event in steps:
            scan, effects = ap.step(scan, event)
            history.append((scan, tuple(effects)))
        return history

    assert run() == run()


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_scanning_is_only_ever_entered_by_scan_requested(steps):
    """The only transition that can move the state to SCANNING is
    ScanRequested; a bare ReadNotFound, however many arrive in a row,
    never does -- there is no elapsed-time path into SCANNING at all."""
    scan = ap.ReaderScan(ap.ScanState.READING)
    for event in steps:
        before = scan
        scan, effects = ap.step(scan, event)
        if scan.state is ap.ScanState.SCANNING and before.state is ap.ScanState.READING:
            assert isinstance(event, ap.ScanRequested)


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_leaving_scanning_only_via_not_found(steps):
    """The only transition that can move the state from SCANNING back to
    READING is a ReadNotFound (the true frontier); ScanRequested while
    already scanning changes nothing, and Data/Tombstone keep scanning."""
    scan = ap.ReaderScan(ap.ScanState.READING)
    for event in steps:
        before = scan
        scan, effects = ap.step(scan, event)
        if before.state is ap.ScanState.SCANNING and scan.state is ap.ScanState.READING:
            assert isinstance(event, ap.ReadNotFound)
            assert effects == [ap.AdoptFrontier()]


@settings(max_examples=300, deadline=None)
@given(steps=st.lists(_EVENTS, min_size=1, max_size=50))
def test_reading_not_found_never_changes_anything(steps):
    """ReadNotFound while READING is always a pure no-op, however many of
    them arrive in a row: it is exactly as indistinguishable from an
    ordinarily quiet stream as the spec says it is, and step() does not
    pretend otherwise by accumulating any hidden state across them."""
    scan = ap.ReaderScan(ap.ScanState.READING)
    for event in steps:
        if scan.state is ap.ScanState.READING and isinstance(event, ap.ReadNotFound):
            new_scan, effects = ap.step(scan, event)
            assert new_scan == scan
            assert effects == []
        else:
            scan, _ = ap.step(scan, event)


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
# GC and her own periodic sweep; Bob reads it with a real ReaderScan, and at
# some point his user requests a scan. This is the closest a pure-Python
# property test gets to the cross-member liveness claim in "Optimistic
# resync": given Alice keeps her own sweep going (online at least once per
# epoch), does a single, deliberate, user-triggered scan always recover
# everything -- no matter how long Bob was offline, and with no assumption
# at all about how often Bob himself checks in?
#
# This is deliberately a small abstraction, not a rebuild of the real GC or
# sweep code: a position "exists" if it was written or swept within the
# current or immediately preceding epoch (mirroring WipeStaleBoxes), and
# Alice's sweep refreshes every position she is online to run it against.
# Bob uses the real ap.step for his own side.
# ---------------------------------------------------------------------------

def _run_joint_simulation(
    n_positions, epoch_length, total_ticks, alice_online, bob_online, scan_request_ticks,
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

    scan = ap.ReaderScan(ap.ScanState.READING)
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

        if tick in scan_request_ticks and scan.state is ap.ScanState.READING:
            scan, _ = ap.step(scan, ap.ScanRequested())
            # The scan's forward probe starts at the position ordinary
            # reading was already stuck on.
            scan_cursor = next_index

        # Once actively Scanning, race through the backlog within this
        # same tick: deriving and probing the next index needs no network
        # round trip (see "Optimistic resync"), so it is not paced the way
        # an ordinary ReadNotFound retry is. While merely READING, one
        # ordinary read attempt per online tick, exactly as always.
        while True:
            probe_index = next_index if scan.state is ap.ScanState.READING else scan_cursor

            if probe_index >= n_positions:
                event = ap.ReadNotFound()
            elif exists(probe_index, tick):
                event = ap.ReadOk(str(probe_index).encode())
            else:
                event = ap.ReadNotFound()

            was_scanning = scan.state is ap.ScanState.SCANNING
            scan, effects = ap.step(scan, event)
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
    Hypothesis wants to add. Used for Alice's side: her periodic refresh
    still needs this to guarantee anything survives to be found."""
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
def test_a_single_requested_scan_recovers_everything_given_alice_stayed_fair(
    n_positions, epoch_length, data,
):
    # Bob is deliberately online nowhere except the single tick where his
    # scan is requested and completes (Scanning races through the whole
    # backlog in one tick -- see _run_joint_simulation): there is no
    # assumption at all about how often Bob himself checks in, only that
    # Alice's own refresh has been keeping things alive.
    slack_epochs = data.draw(st.integers(min_value=3, max_value=20))
    total_epochs = slack_epochs
    total_ticks = total_epochs * epoch_length
    scan_tick = total_ticks - 1

    alice_online = _weakly_fair_schedule(data, total_ticks, epoch_length, total_epochs)
    bob_online = [False] * total_ticks
    bob_online[scan_tick] = True

    scan, next_index, received = _run_joint_simulation(
        n_positions, epoch_length, total_ticks,
        alice_online, bob_online, {scan_tick},
    )
    note(f"final scan={scan!r} next_index={next_index} received={received}")
    assert received == set(range(n_positions)), (
        "a single requested scan did not recover every position Alice ever "
        "wrote, even though her own refresh was online at least once per "
        "epoch throughout"
    )


@settings(max_examples=100, deadline=None)
@given(
    n_positions=st.integers(min_value=1, max_value=12),
    epoch_length=st.integers(min_value=1, max_value=8),
    data=st.data(),
)
def test_a_requested_scan_is_not_vacuously_satisfied_without_alice(
    n_positions, epoch_length, data,
):
    """The converse of the property above, hunted rather than assumed:
    with Alice never online at all, a requested scan still runs to
    completion, but recovers nothing -- confirming the mechanism does not
    unconditionally launder a genuine loss into an apparent success just
    because the user asked."""
    total_epochs = data.draw(st.integers(min_value=3, max_value=20))
    total_ticks = total_epochs * epoch_length
    scan_tick = total_ticks - 1

    alice_online = [False] * total_ticks
    bob_online = [False] * total_ticks
    bob_online[scan_tick] = True

    scan, next_index, received = _run_joint_simulation(
        n_positions, epoch_length, total_ticks,
        alice_online, bob_online, {scan_tick},
    )
    note(f"final scan={scan!r} next_index={next_index} received={received}")
    assert received == set()
