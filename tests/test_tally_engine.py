"""Engine correctness: vote recording, domain validation, the pure tally."""
from __future__ import annotations

import uuid

import pytest

from katzenqt.tally import engine, schema
from katzenqt.tally.schema import Mode


def _sid() -> bytes:
    return uuid.uuid4().bytes


def _by_id(result):
    return {s.slot_id: s for s in result.slots}


def test_approval_counts_absent_slot_as_no():
    doc = schema.new_survey_doc(_sid(), "lunch?", Mode.APPROVAL, ["noon", "one", "two"])
    engine.apply_vote(doc, b"alice", {"s0": "yes", "s1": "no", "s2": "yes"})
    engine.apply_vote(doc, b"bob", {"s0": "yes", "s2": "no"})  # s1 omitted -> no

    res = engine.tally(doc)
    assert res.mode is Mode.APPROVAL
    assert res.n_voters == 2
    by = _by_id(res)
    assert (by["s0"].yes, by["s0"].no, by["s0"].maybe) == (2, 0, 0)
    assert (by["s1"].yes, by["s1"].no, by["s1"].maybe) == (0, 2, 0)
    assert (by["s2"].yes, by["s2"].no, by["s2"].maybe) == (1, 1, 0)


def test_availability_three_way_counts():
    doc = schema.new_survey_doc(_sid(), "meet?", Mode.AVAILABILITY, ["mon", "tue"])
    engine.apply_vote(doc, b"a", {"s0": "yes", "s1": "maybe"})
    engine.apply_vote(doc, b"b", {"s0": "maybe", "s1": "no"})
    engine.apply_vote(doc, b"c", {"s0": "yes"})  # s1 omitted -> no

    by = _by_id(engine.tally(doc))
    assert (by["s0"].yes, by["s0"].maybe, by["s0"].no) == (2, 1, 0)
    assert (by["s1"].yes, by["s1"].maybe, by["s1"].no) == (0, 1, 2)


def test_domain_and_slot_validation():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a"])
    with pytest.raises(ValueError):
        engine.apply_vote(doc, b"a", {"s0": "maybe"})  # maybe illegal in approval
    with pytest.raises(ValueError):
        engine.apply_vote(doc, b"a", {"s9": "yes"})  # unknown slot


def test_revote_overwrites_prior_choice():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a", "b"])
    engine.apply_vote(doc, b"a", {"s0": "yes"})
    engine.apply_vote(doc, b"a", {"s0": "no", "s1": "yes"})

    res = engine.tally(doc)
    assert res.n_voters == 1
    by = _by_id(res)
    assert by["s0"].yes == 0
    assert by["s1"].yes == 1


def test_newer_version_supersedes_prior_vote():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a", "b"])
    engine.apply_vote(doc, b"x", {"s0": "yes"}, version=0)
    engine.apply_vote(doc, b"x", {"s0": "no", "s1": "yes"}, version=1)

    res = engine.tally(doc)
    assert res.n_voters == 1
    by = _by_id(res)
    assert by["s0"].yes == 0
    assert by["s1"].yes == 1


def test_older_version_is_discarded_out_of_order():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a"])
    engine.apply_vote(doc, b"x", {"s0": "yes"}, version=2)
    engine.apply_vote(doc, b"x", {"s0": "no"}, version=1)  # stale, must be ignored

    res = engine.tally(doc)
    assert res.n_voters == 1
    assert res.slots[0].yes == 1  # the version-2 vote stands


def test_current_version_tracks_the_latest():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a"])
    assert engine.current_version(doc, b"x") == -1
    engine.apply_vote(doc, b"x", {"s0": "yes"}, version=0)
    assert engine.current_version(doc, b"x") == 0
    engine.apply_vote(doc, b"x", {"s0": "no"}, version=3)
    assert engine.current_version(doc, b"x") == 3
    engine.apply_vote(doc, b"x", {"s0": "yes"}, version=1)  # stale
    assert engine.current_version(doc, b"x") == 3


def test_close_changes_status():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a"])
    assert engine.tally(doc).status == "open"
    engine.close_survey(doc)
    assert engine.tally(doc).status == "closed"


def test_empty_slots_rejected():
    with pytest.raises(ValueError):
        schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, [])


def test_survey_id_round_trips():
    sid = _sid()
    doc = schema.new_survey_doc(sid, "x", Mode.APPROVAL, ["a"])
    assert engine.tally(doc).survey_id == sid


def test_outcome_declares_a_clear_winner():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a", "b"])
    engine.apply_vote(doc, b"v1", {"s0": "yes"})
    engine.apply_vote(doc, b"v2", {"s0": "yes", "s1": "yes"})
    out = engine.outcome(engine.tally(doc))
    assert out.kind == "winner"
    assert [s.slot_id for s in out.winners] == ["s0"]
    assert out.top_yes == 2


def test_outcome_declares_a_tie():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a", "b"])
    engine.apply_vote(doc, b"v1", {"s0": "yes"})
    engine.apply_vote(doc, b"v2", {"s1": "yes"})
    out = engine.outcome(engine.tally(doc))
    assert out.kind == "tie"
    assert sorted(s.slot_id for s in out.winners) == ["s0", "s1"]
    assert out.top_yes == 1


def test_outcome_no_winner_without_a_single_yes():
    doc = schema.new_survey_doc(_sid(), "x", Mode.APPROVAL, ["a", "b"])
    engine.apply_vote(doc, b"v1", {"s0": "no", "s1": "no"})
    out = engine.outcome(engine.tally(doc))
    assert out.kind == "no_winner"
    assert out.winners == []
    assert out.top_yes == 0
