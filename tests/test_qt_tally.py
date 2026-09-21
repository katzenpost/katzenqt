"""Offscreen widget tests for ``katzenqt.qt_tally`` (the modeless poll window
and the create dialog) and for tally rows rendered by
``katzenqt.qt_models.ConversationLogModel``.

These build QWidgets, so the module-scoped app is a QApplication (see the note
in ``test_conversation_log_model`` about the one-instance rule). Surveys are
seeded straight into the sync DB exactly as ``presenter`` reads them (a
``TallyState`` row holding a ``full_state`` blob) and tally messages as
``ConversationLog`` rows, so no network or io loop is involved.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import models, persistent  # noqa: E402
from katzenqt.qt_models import (  # noqa: E402
    ROLE_CHAT_IS_TALLY,
    ROLE_CHAT_TALLY_KIND,
    ROLE_CHAT_TALLY_SURVEY_ID,
    ConversationLogModel,
)
from katzenqt.qt_tally import (  # noqa: E402
    TallyCreateDialog,
    TallyPanel,
)
from katzenqt.tally import events, schema, sync  # noqa: E402
from katzenqt.tally.controller import voter_id_from_read_cap  # noqa: E402
from katzenqt.tally.schema import Mode  # noqa: E402

ALICE_CAP = bytes([0x02]) * 136
OWN_CAP = bytes([0x01]) * 136


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


def _make_convo_sync(name: str = "lobby") -> "tuple[int, int]":
    """A committed conversation with an own peer ("me") and one active remote
    peer ("alice"), returning ``(conversation_id, own_peer_id)``."""
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
        alice_rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=ALICE_CAP)
        sess.add(alice_rcap)
        sess.add(persistent.ConversationPeer(
            name="alice", read_cap_id=alice_rcap.id, conversation=convo,
        ))
        sess.commit()
        return convo.id, own_peer.id


def _next_order(convo_id: int) -> int:
    with persistent.Session(persistent._engine_sync) as sess:
        rows = sess.exec(
            persistent.select(persistent.ConversationLog)
            .where(persistent.ConversationLog.conversation_id == convo_id)
        ).all()
        return len(rows)


def _seed_chat(convo_id: int, peer_id: int, text: str) -> None:
    cm = models.GroupChatMessage(version=0, membership_hash=bytes(32), text=text)
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            conversation_id=convo_id, conversation_peer_id=peer_id,
            conversation_order=_next_order(convo_id), payload=b"F" + cm.to_cbor(),
        ))
        sess.commit()


def _seed_tally_row(convo_id: int, peer_id: int, gcm) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            conversation_id=convo_id, conversation_peer_id=peer_id,
            conversation_order=_next_order(convo_id),
            payload=b"F" + gcm.to_cbor(),
        ))
        sess.commit()


def _seed_survey(
    convo_id: int,
    survey_id: bytes,
    *,
    topic: str = "lunch?",
    mode: Mode = Mode.APPROVAL,
    slots: "tuple[str, ...]" = ("chicken", "pasta"),
    creator_voter_id: "bytes | None" = None,
):
    doc = schema.new_survey_doc(
        survey_id, topic, mode, slots,
        creator=creator_voter_id or voter_id_from_read_cap(OWN_CAP),
    )
    blob = sync.full_state(doc)
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.TallyState(
            survey_id=survey_id, conversation_id=convo_id, doc_state=blob,
        ))
        sess.commit()
    return doc


# ---------------------------------------------------------------------------
# Tally rows in ConversationLogModel
# ---------------------------------------------------------------------------


def test_create_row_renders_as_a_poll_line():
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    doc = _seed_survey(convo_id, survey_id)
    _seed_tally_row(convo_id, peer_id, events.build_create(survey_id, sync.full_state(doc)))

    m = ConversationLogModel(convo_id)
    m.set_row_count(1)
    idx = m.index(0, 0, QModelIndex())
    assert m.data(idx, ROLE_CHAT_IS_TALLY) is True
    assert m.data(idx, ROLE_CHAT_TALLY_KIND) == "create"
    assert m.data(idx, ROLE_CHAT_TALLY_SURVEY_ID) == survey_id.hex()
    assert m.data(idx, 0) == "me created [Poll] lunch? — open · no votes yet"


def test_vote_row_names_the_sender_and_lists_selections():
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id)
    _seed_tally_row(
        convo_id, peer_id, events.build_vote(survey_id, {"s0": "yes", "s1": "no"}),
    )

    m = ConversationLogModel(convo_id)
    m.set_row_count(1)
    assert m.data(m.index(0, 0, QModelIndex()), 0) == (
        'me voted on "[Poll] lunch?": chicken: yes, pasta: no'
    )
    assert m.data(m.index(0, 0, QModelIndex()), ROLE_CHAT_TALLY_KIND) == "vote"


def test_recast_row_says_changed_vote():
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id)
    _seed_tally_row(
        convo_id, peer_id,
        events.build_vote(survey_id, {"s0": "maybe"}, version=1),
    )
    m = ConversationLogModel(convo_id)
    m.set_row_count(1)
    assert "changed vote in" in m.data(m.index(0, 0, QModelIndex()), 0)
    assert m.data(m.index(0, 0, QModelIndex()), ROLE_CHAT_TALLY_KIND) == "recast"


def test_vote_for_an_unknown_survey_renders_invalid():
    convo_id, peer_id = _make_convo_sync()
    survey_id = uuid.uuid4().bytes  # no survey seeded
    _seed_tally_row(convo_id, peer_id, events.build_vote(survey_id, {"s0": "yes"}))

    m = ConversationLogModel(convo_id)
    m.set_row_count(1)
    assert m.data(m.index(0, 0, QModelIndex()), 0) == f"me: vote for unknown poll {survey_id.hex()}"
    assert m.data(m.index(0, 0, QModelIndex()), ROLE_CHAT_TALLY_KIND) == "invalid"


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


def test_panel_window_title_names_the_conversation_and_topic():
    convo_id, _ = _make_convo_sync("lobby")
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id, topic="dinner?")

    panel = TallyPanel()
    panel.set_conversation_label("lobby")
    assert panel.windowTitle() == "lobby — Poll"
    assert panel.show_survey(convo_id, survey_id) is True
    assert panel.windowTitle() == "lobby — Poll: dinner?"

    panel.clear()
    assert panel.windowTitle() == "lobby — Poll"


def test_panel_clear_drops_the_current_survey():
    convo_id, _ = _make_convo_sync()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id)
    panel = TallyPanel()
    panel.show_survey(convo_id, survey_id)
    assert panel.current_survey() == (convo_id, survey_id)

    panel.clear()
    assert panel.current_survey() is None
    assert panel._topic_label.text() == "No poll selected"
    assert panel._vote_button.isEnabled() is False
    assert panel._close_button.isHidden() is True


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
