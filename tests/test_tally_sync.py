"""The CRDT catch-up path: state vector out, diff back, converge."""
from __future__ import annotations

from katzenqt.tally import engine, schema, sync
from katzenqt.tally.schema import Mode


def _norm(result):
    return [(s.slot_id, s.yes, s.maybe, s.no) for s in result.slots]


def test_late_joiner_catches_up_via_state_vector():
    a = schema.new_survey_doc(b"sid", "x", Mode.APPROVAL, ["p", "q"])
    engine.apply_vote(a, b"alice", {"s0": "yes"})

    # A new peer joins and loads the survey from the broadcast full state.
    b = sync.load_doc(sync.full_state(a))
    assert engine.tally(b).n_voters == 1
    assert _norm(engine.tally(b)) == _norm(engine.tally(a))

    # A moves ahead; B catches up in one exchange from its state vector.
    engine.apply_vote(a, b"bob", {"s1": "yes"})
    diff = sync.diff_since(a, sync.state_vector(b))
    sync.apply_update(b, diff)

    assert engine.tally(b).n_voters == 2
    assert _norm(engine.tally(a)) == _norm(engine.tally(b))


def test_concurrent_edits_merge_symmetrically():
    base = sync.full_state(schema.new_survey_doc(b"sid", "x", Mode.AVAILABILITY, ["p", "q"]))
    a = sync.load_doc(base)
    b = sync.load_doc(base)

    # Disjoint, concurrent votes on each replica.
    engine.apply_vote(a, b"alice", {"s0": "yes", "s1": "maybe"})
    engine.apply_vote(b, b"bob", {"s0": "no", "s1": "yes"})

    # Exchange diffs both ways.
    a_to_b = sync.diff_since(a, sync.state_vector(b))
    b_to_a = sync.diff_since(b, sync.state_vector(a))
    sync.apply_update(b, a_to_b)
    sync.apply_update(a, b_to_a)

    assert engine.tally(a).n_voters == 2
    assert engine.tally(b).n_voters == 2
    assert _norm(engine.tally(a)) == _norm(engine.tally(b))
