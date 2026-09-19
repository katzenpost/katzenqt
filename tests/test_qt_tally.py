"""Offscreen widget tests for ``katzenqt.qt_tally``: the poll timeline model,
the Polls tab list, the voting panel and the create dialog.

These build QWidgets, so the module-scoped app is a QApplication (see the
note in ``test_conversation_log_model`` about the one-instance rule).
Surveys are seeded straight into the sync DB exactly as ``presenter`` reads
them (a ``TallyState`` row holding a ``full_state`` blob), so no network or
io loop is involved.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import models, persistent  # noqa: E402
from katzenqt.qt_tally import (  # noqa: E402
    ROLE_TALLY_NEW,
    ROLE_TALLY_PLACEHOLDER,
    ROLE_TALLY_SURVEY_ID,
    PollsTabModel,
    TallyCreateDialog,
    TallyPanel,
    TimelineModel,
    polls_tab_label,
)
from katzenqt.tally import schema, sync  # noqa: E402
from katzenqt.tally.controller import voter_id_from_read_cap  # noqa: E402
from katzenqt.tally.schema import Mode  # noqa: E402

ALICE_CAP = bytes([0x02]) * 136
OWN_CAP = bytes([0x01]) * 136


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


def _make_convo_sync(name: str = "lobby") -> "tuple[int, int]":
    """A committed conversation with an own peer, returning
    ``(conversation_id, own_peer_id)``."""
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
        sess.commit()
        return convo.id, own_peer.id


def _seed_chat(convo_id: int, peer_id: int, order: int, text: str) -> None:
    cm = models.GroupChatMessage(version=0, membership_hash=bytes(32), text=text)
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            conversation_id=convo_id, conversation_peer_id=peer_id,
            conversation_order=order, payload=b"F" + cm.to_cbor(),
        ))
        sess.commit()


def _seed_survey(
    convo_id: int,
    survey_id: bytes,
    *,
    topic: str = "lunch?",
    mode: Mode = Mode.APPROVAL,
    slots: "tuple[str, ...]" = ("chicken", "pasta"),
    order: "int | None" = 0,
    creator_voter_id: "bytes | None" = None,
) -> None:
    doc = schema.new_survey_doc(
        survey_id, topic, mode, slots,
        creator=creator_voter_id or voter_id_from_read_cap(OWN_CAP),
    )
    blob = sync.full_state(doc)
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=survey_id, conversation_id=convo_id,
            doc_state=blob, conversation_order=order,
        ))
        sess.commit()


def _set_first_unread(convo_id: int, value: int) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, convo_id)
        conv.first_unread = value
        sess.add(conv)
        sess.commit()


def _timeline_for(convo_id: int, n_chat: int = 0) -> TimelineModel:
    m = TimelineModel(convo_id)
    m.source_model().row_count = n_chat  # set by katzen.py:1933 in the GUI
    m.refresh()
    return m


# ---------------------------------------------------------------------------
# TimelineModel
# ---------------------------------------------------------------------------


def test_timeline_interleaves_polls_and_chat_and_sorts_poll_first_on_equal_order():
    """A survey stamped at order O while the log holds O rows shares order O
    with the next chat row; the poll sorts ahead (it came first)."""
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_chat(convo_id, peer_id, 0, "hello 0")
    _seed_chat(convo_id, peer_id, 1, "hello 1")
    _seed_survey(convo_id, survey_id, order=1)

    m = _timeline_for(convo_id, n_chat=2)

    assert [e.kind for e in m._rows] == ["chat", "poll", "chat"]
    assert [e.order for e in m._rows] == [0, 1, 1]

    chat0, poll, chat1 = (m.index(r, 0) for r in range(3))
    assert m.data(chat0, 0) == "hello 0"
    assert m.data(poll, 0) == "[Poll] lunch? — open · no votes yet"
    assert m.data(chat1, 0) == "hello 1"

    assert m.data(poll, ROLE_TALLY_PLACEHOLDER) is True
    assert m.data(poll, ROLE_TALLY_SURVEY_ID) == survey_id.hex()
    assert m.data(chat0, ROLE_TALLY_PLACEHOLDER) is None
    assert m.data(chat0, ROLE_TALLY_SURVEY_ID) is None

    # A poll is "new" once its order passes the first-unread pointer.
    m.set_first_unread(2)
    assert m.data(poll, ROLE_TALLY_NEW) is False
    m.set_first_unread(0)
    assert m.data(poll, ROLE_TALLY_NEW) is True


def test_timeline_untamped_survey_hangs_off_the_tail():
    """A survey whose order was never persisted renders after every chat row."""
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    for i, text in enumerate(("a", "b", "c")):
        _seed_chat(convo_id, peer_id, i, text)
    _seed_survey(convo_id, survey_id, order=None)

    m = _timeline_for(convo_id, n_chat=3)

    assert [e.kind for e in m._rows] == ["chat", "chat", "chat", "poll"]
    assert m._rows[-1].order == 3
    assert m.survey_id_at_row(3) == survey_id.hex()
    assert m.survey_id_at_row(0) is None
    assert m.survey_id_at_row(-1) is None


def test_timeline_unread_maps_between_row_and_order_space():
    """QML's first_unread counter is row space; the DB pointer is order space."""
    convo_id, peer_id = _make_convo_sync()
    _seed_chat(convo_id, peer_id, 0, "a")
    _seed_survey(convo_id, uuid.uuid4().bytes, order=1)  # shares order 1
    _seed_chat(convo_id, peer_id, 1, "b")

    m = _timeline_for(convo_id, n_chat=2)

    assert [e.order for e in m._rows] == [0, 1, 1]
    assert m.order_to_row(0) == 0
    assert m.order_to_row(1) == 1  # the poll row
    assert m.order_to_row(2) == 3  # all read

    assert m.row_to_order(0) == 0
    assert m.row_to_order(1) == 1
    assert m.row_to_order(len(m._rows)) == 2  # past-the-end
    assert m.row_to_order(-1) == 0

    m.set_first_unread(1)
    assert m.first_unread_row == 1
    assert m.first_unread_order == 1


def test_timeline_unknown_conversation_has_no_rows():
    m = _timeline_for(0xFFFFFF)
    assert m.rowCount() == 0
    assert m.order_to_row(0) == 0
    assert m.row_to_order(0) == 0


# ---------------------------------------------------------------------------
# Polls tab model
# ---------------------------------------------------------------------------


def test_polls_tab_model_filters_by_conversation_and_counts_badges():
    alpha_id, _ = _make_convo_sync("alpha")
    beta_id, _ = _make_convo_sync("beta")
    a_sid = uuid.uuid4().bytes
    b_sid = uuid.uuid4().bytes
    _seed_survey(alpha_id, a_sid, topic="tea?", order=0)
    _seed_survey(beta_id, b_sid, topic="beer?", order=0)
    _set_first_unread(alpha_id, 99)  # read the alpha poll

    model = PollsTabModel()
    model.refresh()
    assert model.rowCount() == 2
    assert model.badge_count() == 1  # only the beta poll is new

    model.set_conversation_filter(alpha_id)
    assert model.rowCount() == 1
    assert model.data(model.index(0), 0x201) == "tea?"
    assert model.data(model.index(0), 0x204) == alpha_id
    assert model.data(model.index(0), 0x205) == "alpha"
    assert model.data(model.index(0), 0x208) is False
    assert model.summary_at(0).topic == "tea?"

    model.set_conversation_filter(None)
    assert model.rowCount() == 2
    assert model.badge_count() == 1


def test_polls_tab_label_attaches_the_badge():
    assert polls_tab_label(0) == "Polls"
    assert polls_tab_label(3) == "Polls (3)"


# ---------------------------------------------------------------------------
# TallyPanel
# ---------------------------------------------------------------------------


def test_panel_renders_a_survey_and_remembers_it_as_current():
    convo_id, _ = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id, topic="dinner?", slots=("tacos", "sushi"))

    panel = TallyPanel()
    assert panel.current_survey() is None
    assert panel.show_survey(convo_id, survey_id) is True
    assert panel.current_survey() == (convo_id, survey_id)

    assert panel._topic_label.text() == "dinner?"
    assert panel._status_label.text() == "Open"
    assert "approval" in panel._meta_label.text()
    assert "2 slot(s)" in panel._meta_label.text()

    assert panel.show_survey(convo_id, uuid.uuid4().bytes) is False
    assert panel.current_survey() == (convo_id, survey_id)


def test_panel_cycles_slots_and_submits_its_selection():
    convo_id, _ = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id)

    panel = TallyPanel()
    assert panel.show_survey(convo_id, survey_id) is True

    assert panel.selection == {}
    assert panel._vote_button.isEnabled() is False

    fired: "list[dict]" = []
    panel.voteSubmitted.connect(fired.append)

    panel._slot_buttons["s0"].click()
    assert panel.selection == {"s0": "yes"}
    assert panel._vote_button.isEnabled() is True

    panel._slot_buttons["s0"].click()  # yes -> no
    assert panel.selection == {"s0": "no"}
    panel._slot_buttons["s0"].click()  # no -> blank (deselect)
    assert panel.selection == {}
    assert panel._vote_button.isEnabled() is False

    panel._slot_buttons["s1"].click()
    panel._slot_buttons["s1"].click()
    panel._vote_button.click()
    assert fired == [{"s1": "no"}]
    assert panel._vote_button.isEnabled() is False  # submitted == current


def test_panel_close_button_only_for_the_creator_and_emits_close_requested():
    convo_id, _ = _make_convo_sync()
    mine = uuid.uuid4().bytes
    alice = uuid.uuid4().bytes
    _seed_survey(convo_id, mine, topic="mine")
    _seed_survey(
        convo_id, alice, topic="theirs",
        creator_voter_id=voter_id_from_read_cap(ALICE_CAP),
    )

    panel = TallyPanel()
    panel.show_survey(convo_id, alice)
    assert panel._close_button.isHidden() is True

    panel.show_survey(convo_id, mine)
    assert panel._close_button.isHidden() is False
    fired: "list[bool]" = []
    panel.closeRequested.connect(lambda: fired.append(True))
    panel._close_button.click()
    assert fired == [True]


def test_panel_new_poll_button_emits_new_poll_requested():
    panel = TallyPanel()
    fired: "list[bool]" = []
    panel.newPollRequested.connect(lambda: fired.append(True))
    panel._new_poll_button.click()
    assert fired == [True]


# ---------------------------------------------------------------------------
# TallyCreateDialog
# ---------------------------------------------------------------------------


def test_create_dialog_gathers_topic_slots_and_mode():
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = TallyCreateDialog()
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert ok_button.isEnabled() is False  # blank topic, no slots

    dialog._topic.setText("next meeting")
    assert ok_button.isEnabled() is False  # still no slots

    dialog._slot_input.setText("  thursday  ")
    dialog._add_custom_slot()
    assert dialog.slots() == ["thursday"]
    assert ok_button.isEnabled() is True

    dialog._calendar.setSelectedDate(dialog._calendar.selectedDate())
    dialog._add_calendar_slot()
    assert len(dialog.slots()) == 2
    assert dialog.slots()[1] == dialog._calendar.selectedDate().toString("ddd d MMM")

    assert dialog.topic() == "next meeting"
    assert dialog.mode() is Mode.APPROVAL


def test_create_dialog_removes_and_reorders_custom_slots():
    dialog = TallyCreateDialog()
    for text in ("a", "b", "c"):
        dialog._slot_input.setText(text)
        dialog._add_custom_slot()

    dialog._slots_list.setCurrentRow(1)
    dialog._move_selected(1)
    assert dialog.slots() == ["a", "c", "b"]

    dialog._slots_list.setCurrentRow(2)
    dialog._move_selected(-1)
    assert dialog.slots() == ["a", "b", "c"]

    dialog._slots_list.setCurrentRow(1)
    dialog._remove_selected()
    assert dialog.slots() == ["a", "c"]