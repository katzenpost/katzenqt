"""The Qt-free projection of surveys into GUI-ready data.

``presenter`` is the single place that turns a survey ``Doc`` (plus persisted
identity) into what the chat timeline, the poll panel and the tab badge show.
Everything here must stay importable without PySide6 (see
``tests/test_qt_decoupling.py``).
"""
from __future__ import annotations

import uuid

from katzenqt import persistent
from katzenqt.tally import engine, presenter, schema
from katzenqt.tally.controller import voter_id_from_read_cap
from katzenqt.tally.schema import Mode

ALICE_CAP = bytes([0x02]) * 136
BOB_CAP = bytes([0x03]) * 136
OWN_CAP = bytes([0x01]) * 136


def _doc(mode: Mode = Mode.APPROVAL, slots=("a", "b")):
    return schema.new_survey_doc(uuid.uuid4().bytes, "lunch?", mode, slots)


def _make_convo_sync(name="g"):
    """A conversation + own peer + two active peers, provisioned with read
    caps, committed through the sync engine the presenter reads."""
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcap.id, read_cap=OWN_CAP,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=own_rcap.id, conversation=convo,
    )
    convo.own_peer = own_peer
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        for pname, cap in (("alice", ALICE_CAP), ("bob", BOB_CAP)):
            rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=cap)
            peer = persistent.ConversationPeer(
                name=pname, read_cap_id=rcap.id, conversation=convo,
            )
            sess.add(rcap)
            sess.add(peer)
        sess.commit()
        return convo.id


def test_summarize_projects_topic_counts_and_outcome():
    doc = _doc()
    engine.apply_vote(doc, voter_id_from_read_cap(ALICE_CAP), {"s0": "yes"})

    summary = presenter.summarize(
        doc, conversation_id=1, conversation_order=3,
        my_voter_id=voter_id_from_read_cap(OWN_CAP),
    )
    assert summary.topic == "lunch?"
    assert summary.mode is Mode.APPROVAL
    assert summary.status == "open"
    assert summary.n_slots == 2
    assert summary.n_voters == 1
    assert summary.conversation_order == 3
    assert summary.slots[0].yes == 1
    assert summary.outcome.kind == "winner"
    # The local user has not voted yet.
    assert summary.my_choices == {}


def test_summarize_records_my_choices_from_my_voter_id():
    doc = _doc()
    engine.apply_vote(doc, voter_id_from_read_cap(OWN_CAP), {"s0": "yes"})

    summary = presenter.summarize(
        doc, conversation_id=1, my_voter_id=voter_id_from_read_cap(OWN_CAP),
    )
    assert summary.my_choices == {"s0": "yes"}
    assert summary.voted_slot_ids() == ["s0"]
    assert summary.my_score_on("s1") is None


def test_placeholder_text_reflects_state_and_participation():
    doc = _doc()
    open_none = presenter.summarize(doc, conversation_id=1)
    assert presenter.placeholder_text(open_none) == "[Poll] lunch? — open · no votes yet"

    engine.apply_vote(doc, voter_id_from_read_cap(ALICE_CAP), {"s0": "yes"})
    open_one = presenter.summarize(doc, conversation_id=1)
    assert presenter.placeholder_text(open_one) == "[Poll] lunch? — open · 1/2 voted"

    engine.close_survey(doc)
    closed = presenter.summarize(doc, conversation_id=1)
    assert presenter.placeholder_text(closed) == "[Poll] lunch? — closed"


def test_panel_rows_map_voter_ids_to_names_and_unknowns():
    doc = _doc()
    engine.apply_vote(doc, voter_id_from_read_cap(ALICE_CAP), {"s0": "yes"})
    engine.apply_vote(doc, b"\xaa" * 16, {"s1": "no"})  # not a known member

    rows = presenter.panel_rows(doc, {voter_id_from_read_cap(ALICE_CAP): "alice"})
    by_name = {r.name: r for r in rows}
    assert by_name["alice"].choices == {"s0": "yes"}
    assert by_name[presenter._UNKNOWN_VOTER].choices == {"s1": "no"}
    assert by_name["alice"].line(doc_slots(doc)) == "alice: a: yes"


def doc_slots(doc):
    return tuple(presenter.summarize(doc, conversation_id=1).slots)


def test_own_voter_id_and_voter_names_read_the_group_identity():
    convo_id = _make_convo_sync()
    me = presenter.own_voter_id(convo_id)
    assert me is not None and me == voter_id_from_read_cap(OWN_CAP)

    names = presenter.voter_names(convo_id)
    assert names[voter_id_from_read_cap(OWN_CAP)] == "me"
    assert names[voter_id_from_read_cap(ALICE_CAP)] == "alice"
    assert names[voter_id_from_read_cap(BOB_CAP)] == "bob"


def test_surveys_for_conversation_returns_first_sighting_order():
    convo_id = _make_convo_sync()
    first = uuid.uuid4().bytes
    second = uuid.uuid4().bytes
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=second, conversation_id=convo_id, doc_state=b"x",
            conversation_order=3,
        ))
        sess.add(persistent.TallyState(
            survey_id=first, conversation_id=convo_id, doc_state=b"x",
            conversation_order=1,
        ))
        sess.commit()

    got = presenter.surveys_for_conversation(convo_id)
    assert got == [(first, 1), (second, 3)]


def test_all_survey_ids_lists_every_conversation():
    one = _make_convo_sync("one")
    two = _make_convo_sync("two")
    s1, s2 = uuid.uuid4().bytes, uuid.uuid4().bytes
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=s1, conversation_id=one, doc_state=b"x", conversation_order=0,
        ))
        sess.add(persistent.TallyState(
            survey_id=s2, conversation_id=two, doc_state=b"x", conversation_order=0,
        ))
        sess.commit()

    got = presenter.all_survey_ids()
    assert (one, s1, 0) in got and (two, s2, 0) in got


def test_badge_count_counts_new_surveys():
    summary = presenter.summarize(_doc(), conversation_id=1)
    old = presenter.summarize(_doc(), conversation_id=1)
    assert presenter.badge_count([summary, old]) == 0
    assert presenter.badge_count([
        presenter.summarize(_doc(), conversation_id=1, is_new=True),
        old,
    ]) == 1


def test_is_creator_only_for_the_creator_voter():
    doc = _doc()
    # A survey we created carries our voter id as its creator.
    doc_self = schema.new_survey_doc(
        uuid.uuid4().bytes, "x", Mode.APPROVAL, ["a"],
        creator=voter_id_from_read_cap(OWN_CAP),
    )
    mine = presenter.summarize(
        doc_self, conversation_id=1, my_voter_id=voter_id_from_read_cap(OWN_CAP),
    )
    assert mine.is_creator() is True

    theirs = presenter.summarize(
        doc, conversation_id=1, my_voter_id=voter_id_from_read_cap(OWN_CAP),
    )
    assert theirs.is_creator() is False