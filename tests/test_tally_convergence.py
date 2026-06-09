"""Two peers applying the same vote events in any order converge.

A vote arrives as a semantic event; each peer writes ``votes[sender] = choice``
into its own ``Doc``. With one event per voter the final state, and so the
tally, is independent of arrival order. This is the property a global passive
reordering of the chat must not be able to disturb.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from katzenqt.tally import engine, schema
from katzenqt.tally.schema import Mode


def _norm(result):
    return [(s.slot_id, s.yes, s.maybe, s.no) for s in result.slots]


_SLOTS = ["a", "b", "c"]
_SLOT_IDS = [schema.slot_id(i) for i in range(len(_SLOTS))]


def _choice_strategy(domain):
    # A partial map: each slot independently present (with an availability) or
    # absent (which tallies as "no").
    return st.dictionaries(
        keys=st.sampled_from(_SLOT_IDS),
        values=st.sampled_from(domain),
        max_size=len(_SLOT_IDS),
    )


@settings(max_examples=50, deadline=None)
@given(
    mode=st.sampled_from(list(Mode)),
    data=st.data(),
)
def test_event_order_does_not_change_tally(mode, data):
    domain = schema.domain(mode)
    n = data.draw(st.integers(min_value=0, max_value=6))
    # Unique voter ids so each votes exactly once.
    events = [
        (f"voter{i}".encode(), data.draw(_choice_strategy(domain)))
        for i in range(n)
    ]
    order = data.draw(st.permutations(list(range(n))))

    doc_a = schema.new_survey_doc(b"survey", "t", mode, _SLOTS)
    doc_b = schema.new_survey_doc(b"survey", "t", mode, _SLOTS)
    for voter, choice in events:
        engine.apply_vote(doc_a, voter, choice)
    for i in order:
        voter, choice = events[i]
        engine.apply_vote(doc_b, voter, choice)

    res_a, res_b = engine.tally(doc_a), engine.tally(doc_b)
    assert res_a.n_voters == res_b.n_voters == n
    assert _norm(res_a) == _norm(res_b)
