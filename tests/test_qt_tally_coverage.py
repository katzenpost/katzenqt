from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import persistent  # noqa: E402
from katzenqt.qt_tally import (  # noqa: E402
    TallyCreateDialog,
    TallyPanel,
    conversation_first_unread,
    outcome_text,
)
from katzenqt.tally import schema, sync  # noqa: E402
from katzenqt.tally.controller import voter_id_from_read_cap  # noqa: E402
from katzenqt.tally.engine import Outcome, SlotTally  # noqa: E402

OWN_CAP = bytes([0x01]) * 136
MIDDLE_DOT = "\u00b7"


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


def _make_conversation(name: str = "lobby", first_unread: int = 0) -> int:
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        write_cap_id=wcap.id,
        read_cap=OWN_CAP,
    )
    convo = persistent.Conversation(
        name=name,
        write_cap=wcap.id,
        first_unread=first_unread,
    )
    own_peer = persistent.ConversationPeer(
        name="me",
        read_cap_id=own_rcap.id,
        conversation=convo,
    )
    convo.own_peer = own_peer
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        sess.commit()
        conv_id: int = convo.id
        return conv_id


def _seed_survey(convo_id: int, survey_id: bytes) -> None:
    doc = schema.new_survey_doc(
        survey_id,
        "lunch?",
        schema.Mode.APPROVAL,
        ("chicken", "pasta"),
        creator=voter_id_from_read_cap(OWN_CAP),
    )
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=convo_id,
                doc_state=sync.full_state(doc),
            )
        )
        sess.commit()


def test_outcome_text_reports_a_tie_by_option_name() -> None:
    outcome = Outcome(
        kind="tie",
        winners=[
            SlotTally(slot_id="s0", text="chicken", yes=2, maybe=0, no=1),
            SlotTally(slot_id="s1", text="", yes=2, maybe=0, no=1),
        ],
        top_yes=2,
    )
    assert outcome_text(outcome) == "Tie: chicken / s1 (2 yes each)."


def test_outcome_text_handles_a_winner_kind_with_no_winners() -> None:
    assert (
        outcome_text(
            Outcome(kind="winner", winners=[], top_yes=1),
        )
        == "No winner yet."
    )


def test_slot_totals_mentions_maybe_only_when_it_is_used() -> None:
    with_maybe = SlotTally(slot_id="s0", text="chicken", yes=2, maybe=1, no=3)
    without = SlotTally(slot_id="s1", text="pasta", yes=2, maybe=0, no=3)
    assert TallyPanel._slot_totals(with_maybe) == (
        f"yes 2 {MIDDLE_DOT} maybe 1 {MIDDLE_DOT} no 3"
    )
    assert TallyPanel._slot_totals(without) == f"yes 2 {MIDDLE_DOT} no 3"


def test_a_panel_with_no_survey_ignores_edit_and_submit() -> None:
    panel = TallyPanel()
    fired: list[object] = []
    panel.voteSubmitted.connect(fired.append)

    panel._rebuild_grid()
    assert panel._grid.count() == 0

    panel._start_editing()
    assert panel._editing is False

    panel._submit()
    assert fired == []


def test_refreshing_the_same_survey_keeps_an_in_progress_edit() -> None:
    convo_id = _make_conversation()
    survey_id = uuid.uuid4().bytes
    _seed_survey(convo_id, survey_id)

    panel = TallyPanel()
    assert panel.show_survey(convo_id, survey_id) is True
    assert panel._editing is True
    panel._slot_buttons["s0"].click()
    assert panel.selection == {"s0": "yes"}

    assert panel.show_survey(convo_id, survey_id) is True
    assert panel.selection == {"s0": "yes"}
    assert panel._vote_button.isEnabled() is True


def test_move_selected_is_a_no_op_off_either_end() -> None:
    dialog = TallyCreateDialog()
    dialog._move_selected(-1)
    assert dialog.slots() == []

    for text in ("a", "b"):
        dialog._slot_input.setText(text)
        dialog._add_custom_slot()

    dialog._slots_list.setCurrentRow(0)
    dialog._move_selected(-1)
    assert dialog.slots() == ["a", "b"]

    dialog._slots_list.setCurrentRow(1)
    dialog._move_selected(1)
    assert dialog.slots() == ["a", "b"]


def test_conversation_first_unread_reads_the_persisted_pointer() -> None:
    convo_id = _make_conversation("lobby", first_unread=3)
    assert conversation_first_unread(convo_id) == 3
    assert conversation_first_unread(convo_id + 1000) == 0
