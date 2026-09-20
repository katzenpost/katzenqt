"""Qt-only models and widgets for the tally/poll GUI.

Everything here imports PySide6, so nothing in ``katzenqt``, ``persistent``,
``network`` or ``models`` may import it (see tests/test_qt_decoupling.py); the
tally protocol derivation stays in ``katzenqt.tally.presenter`` / ``engine``,
which this module projects into Qt models and widgets.

Two pieces:

* :class:`PollsTabModel`: the flat list behind the Polls sibling tab. (Tally
  messages are ordinary chat rows; their display is derived in
  :mod:`katzenqt.qt_models` via :mod:`katzenqt.tally.presenter`.)
* :class:`TallyPanel` and :class:`TallyCreateDialog`: the embedded poll panel
  (click-to-cycle voting grid) and the hybrid create dialog (custom options
  + date picks).

The panel's voting grid is deliberately dumb: it edits a local selection map
and emits it as a plain ``dict`` on :attr:`TallyPanel.voteSubmitted`; wiring
(io-loop persistence through ``katzenqt.tally.controller``) is the caller's
job, in the same `run_in_io` style the chat composer uses.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore
from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .tally import presenter, schema
from .tally.engine import Outcome
from .tally.presenter import SurveySummary
from .tally.sync import load_doc

# ---------------------------------------------------------------------------
# Polls tab model
# ---------------------------------------------------------------------------


class PollsTabModel(QtCore.QAbstractListModel):
    """Flat list of every poll (optionally one conversation's) for the Polls
    sibling tab. Each row carries enough plain data to render a compact card
    and to open the full :class:`TallyPanel` on click."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: "list[SurveySummary]" = []
        self._conversation_id: "int | None" = None
        self._names: "dict[int, str]" = presenter.conversation_names()

    def set_conversation_filter(self, conversation_id: "int | None") -> None:
        """Limit to one conversation's polls (None shows all conversations)."""
        self._conversation_id = conversation_id
        self.refresh()

    def refresh(self) -> None:
        """Re-read the persisted surveys (call after tally notifications)."""
        self._names = presenter.conversation_names()
        ids = (
            presenter.all_survey_ids()
            if self._conversation_id is None
            else [
                (self._conversation_id, sid)
                for sid in presenter.survey_ids_for_conversation(self._conversation_id)
            ]
        )
        rows: "list[SurveySummary]" = []
        names_by_convo: "dict[int, dict[bytes, str]]" = {}
        for conversation_id, survey_id in ids:
            blob = presenter.survey_doc(conversation_id, survey_id)
            if blob is None:
                continue
            names = names_by_convo.get(conversation_id)
            if names is None:
                names = presenter.voter_names(conversation_id)
                names_by_convo[conversation_id] = names
            summary = presenter.summarize(
                load_doc(blob),
                conversation_id=conversation_id,
                my_voter_id=presenter.own_voter_id(conversation_id),
                voter_names=names,
            )
            rows.append(summary)
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def badge_count(self) -> int:
        """Unread poll-create messages for the current filter, for the tab
        badge."""
        if self._conversation_id is not None:
            return presenter.new_poll_count(self._conversation_id)
        return sum(
            presenter.new_poll_count(cid) for cid in presenter.conversation_ids()
        )

    def summary_at(self, row: int) -> "SurveySummary | None":
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent: "QModelIndex | None" = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def roleNames(self) -> dict:
        return {
            0: b"display",
            0x200: b"poll_id",
            0x201: b"topic",
            0x202: b"status",
            0x203: b"mode",
            0x204: b"conversation_id",
            0x205: b"conversation_name",
            0x206: b"n_voters",
            0x207: b"n_slots",
        }

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        s = self._rows[index.row()]
        if role == 0:
            return f"[Poll] {s.topic} — {s.status}"
        if role == 0x200:
            return s.survey_id.hex()
        if role == 0x201:
            return s.topic
        if role == 0x202:
            return s.status
        if role == 0x203:
            return s.mode.value
        if role == 0x204:
            return s.conversation_id
        if role == 0x205:
            return self._names.get(s.conversation_id, f"#{s.conversation_id}")
        if role == 0x206:
            return s.n_voters
        if role == 0x207:
            return s.n_slots
        return None


def polls_tab_label(new_count: int) -> str:
    """The Polls tab title, with an attention badge when anything is new."""
    return "Polls" if new_count == 0 else f"Polls ({new_count})"


# ---------------------------------------------------------------------------
# Poll panel
# ---------------------------------------------------------------------------


def outcome_text(outcome: Outcome) -> str:
    """One-line declaration of a survey's current result."""
    if outcome.kind == "no_winner":
        return "No winner yet — no slot has any yes votes."
    if outcome.kind == "tie":
        names = " / ".join(s.text or s.slot_id for s in outcome.winners)
        return f"Tie: {names} ({outcome.top_yes} yes each)."
    winner = outcome.winners[0] if outcome.winners else None
    if winner is None:
        return "No winner yet."
    return f"Leading: {winner.text or winner.slot_id} ({outcome.top_yes} yes)."


class TallyPanel(QWidget):
    """Embedded poll panel: header, per-slot totals, a click-to-cycle voting
    grid, the per-voter detail and (for the creator) a Close button.

    The grid edits a local selection and emits it on :attr:`voteSubmitted` as
    ``{slot_id: availability}`` for the caller to persist on the io loop. It
    never writes the database itself."""

    voteSubmitted = Signal(dict)  # slot_id -> availability (only set slots)
    closeRequested = Signal()
    newPollRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._summary: "SurveySummary | None" = None
        self._survey_key: "tuple[int, bytes] | None" = None
        self._submit_base: "dict[str, str]" = {}
        self._selection: "dict[str, str]" = {}
        self._cycle: "list[str]" = []  # availabilities to cycle (no blank)
        self._slot_text: "dict[str, str]" = {}
        self._slot_buttons: "dict[str, QToolButton]" = {}
        self._voters_text = ""

        self._topic_label = QLabel("No poll selected")
        self._topic_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self._status_label = QLabel("")
        self._meta_label = QLabel("")
        self._outcome_label = QLabel("")
        self._grid = QGridLayout()
        self._voters_label = QLabel("")
        self._voters_label.setWordWrap(True)

        self._vote_button = QPushButton("Send vote")
        self._vote_button.setEnabled(False)
        self._vote_button.clicked.connect(self._submit)
        self._close_button = QPushButton("Close poll")
        self._close_button.hide()
        self._close_button.clicked.connect(self.closeRequested)
        self._voter_detail_button = QPushButton("Show who voted")
        self._voter_detail_button.setCheckable(True)
        self._voter_detail_button.toggled.connect(
            lambda on: self._voters_label.setVisible(on)
        )
        self._voters_label.setVisible(False)
        self._new_poll_button = QPushButton("New poll")
        self._new_poll_button.clicked.connect(self.newPollRequested)

        buttons = QHBoxLayout()
        buttons.addWidget(self._vote_button)
        buttons.addWidget(self._new_poll_button)
        buttons.addStretch(1)
        buttons.addWidget(self._voter_detail_button)
        buttons.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._topic_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._meta_label)
        layout.addWidget(self._outcome_label)
        layout.addLayout(self._grid)
        layout.addLayout(buttons)
        layout.addWidget(self._voters_label)

    # -- population ----------------------------------------------------------

    def set_summary(self, summary: SurveySummary) -> None:
        """Render a survey; grid buttons reset to the summary's own vote."""
        self._summary = summary
        self._cycle = list(schema.domain(summary.mode))
        self._selection = dict(summary.my_choices)
        self._submit_base = dict(summary.my_choices)

        self._set_header(summary)
        self._rebuild_grid(summary)
        self._update_vote_enabled()

        self._voters_label.setText(self._voters_text or "")
        is_open = summary.status == "open"
        self._close_button.setVisible(is_open and summary.is_creator())

    def set_voters(self, voters: "tuple[presenter.VoterRow, ...]") -> None:
        """The per-voter detail lines (caller resolves names via
        presenter.voter_names + presenter.panel_rows)."""
        self._voters_text = "\n".join(v.line(self._summary.slots) for v in voters)
        self._voters_label.setText(self._voters_text)

    def show_survey(self, conversation_id: int, survey_id: bytes) -> bool:
        """Load one survey from persisted state and render it (summary, grid
        and per-voter detail). Returns False when the survey is unknown;
        otherwise records it as the current survey so a later refresh (a
        received vote or close) can re-render the same one."""
        blob = presenter.survey_doc(conversation_id, survey_id)
        if blob is None:
            return False
        doc = load_doc(blob)
        names = presenter.voter_names(conversation_id)
        summary = presenter.summarize(
            doc,
            conversation_id=conversation_id,
            my_voter_id=presenter.own_voter_id(conversation_id),
            voter_names=names,
        )
        self._survey_key = (conversation_id, survey_id)
        self.set_summary(summary)
        self.set_voters(presenter.panel_rows(doc, names))
        return True

    def current_survey(self) -> "tuple[int, bytes] | None":
        """``(conversation_id, survey_id)`` of the poll being shown, so a
        refresh can re-render it (e.g. on a received TALLY_CLOSE), else None."""
        return self._survey_key

    def clear(self) -> None:
        """Drop the current survey (e.g. when the selected conversation
        changes so a poll from another conversation is not left on screen)."""
        self._survey_key = None
        self._summary = None
        self._submit_base = {}
        self._selection = {}
        self._cycle = []
        self._voters_text = ""
        self._clear_grid()
        self._topic_label.setText("No poll selected")
        self._status_label.setText("")
        self._meta_label.setText("")
        self._outcome_label.setText("")
        self._voters_label.setText("")
        self._vote_button.setEnabled(False)
        self._close_button.hide()

    # -- rendering helpers -----------------------------------------------------

    def _set_header(self, summary: SurveySummary) -> None:
        self._topic_label.setText(summary.topic)
        self._status_label.setText(
            "Open" if summary.status == "open" else "Closed"
        )
        self._meta_label.setText(
            f"{summary.mode.value} · {summary.n_slots} slot(s) · "
            f"{summary.n_voters} voted"
        )
        self._outcome_label.setText(outcome_text(summary.outcome))

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._slot_buttons.clear()
        self._slot_text.clear()

    def _rebuild_grid(self, summary: SurveySummary) -> None:
        self._clear_grid()
        for i, slot in enumerate(summary.slots):
            self._slot_text[slot.slot_id] = slot.text or slot.slot_id
            totals = f"yes {slot.yes}"
            if slot.maybe:
                totals += f" · maybe {slot.maybe}"
            totals += f" · no {slot.no}"
            label = QLabel(self._slot_text[slot.slot_id])
            label.setToolTip(f"{totals}")
            button = QToolButton()
            button.setToolTip(totals)
            self._slot_buttons[slot.slot_id] = button
            self._refresh_button(slot.slot_id)
            self._grid.addWidget(label, i, 0)
            self._grid.addWidget(button, i, 1)
            button.clicked.connect(
                lambda _=False, sid=slot.slot_id: self._cycle_slot(sid)
            )
            button.setEnabled(summary.status == "open")

    def _refresh_button(self, slot_id: str) -> None:
        avail = self._selection.get(slot_id)
        label = self._slot_text.get(slot_id, slot_id)
        self._slot_buttons[slot_id].setText(
            label if avail is None else f"{label}: {avail}"
        )

    # -- interaction ------------------------------------------------------------

    def _cycle_slot(self, slot_id: str) -> None:
        values = ["", *self._cycle]  # blank first, then yes..no (maybe..)
        cur = self._selection.get(slot_id, "")
        nxt = values[(values.index(cur) + 1) % len(values)]
        if nxt:
            self._selection[slot_id] = nxt
        else:
            self._selection.pop(slot_id, None)
        self._refresh_button(slot_id)
        self._update_vote_enabled()

    @property
    def selection(self) -> "dict[str, str]":
        return dict(self._selection)

    def _update_vote_enabled(self) -> None:
        enabled = False
        if self._summary is not None and self._summary.status == "open":
            enabled = self._selection != self._submit_base
        self._vote_button.setEnabled(enabled)

    def _submit(self) -> None:
        if self._vote_button.isEnabled():
            self._submit_base = dict(self._selection)
            self.voteSubmitted.emit(dict(self._selection))
            self._update_vote_enabled()


# ---------------------------------------------------------------------------
# Create dialog
# ---------------------------------------------------------------------------

_DATE_FORMAT = "ddd d MMM"


class TallyCreateDialog(QDialog):
    """Build a survey: a topic, a mode, and slots gathered on two tabs (typed
    custom options, or calendar dates). Read the result via ``topic()`` /
    ``mode()`` / ``slots()``."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New poll")
        self._slots: "list[str]" = []

        self._topic = QLineEdit()
        self._topic.setPlaceholderText("What are we deciding?")
        self._topic.textChanged.connect(self._update_ok)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("approval (yes / no)", schema.Mode.APPROVAL)
        self._mode_combo.addItem("availability (yes / maybe / no)", schema.Mode.AVAILABILITY)

        self._slots_list = QListWidget()
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_custom_tab(), "Custom options")
        self._tabs.addTab(self._build_dates_tab(), "Dates")

        form = QHBoxLayout()
        form.addWidget(QLabel("Topic:"))
        form.addWidget(self._topic, 1)
        form.addWidget(QLabel("Mode:"))
        form.addWidget(self._mode_combo)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._buttons)
        self._update_ok()

    def topic(self) -> str:
        return self._topic.text().strip()

    def mode(self) -> schema.Mode:
        return self._mode_combo.currentData()

    def slots(self) -> "list[str]":
        return list(self._slots)

    # -- slot editing ----------------------------------------------------------

    def _build_custom_tab(self) -> QWidget:
        page = QWidget()
        self._slot_input = QLineEdit()
        self._slot_input.setPlaceholderText("Option text")
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_custom_slot)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove_selected)
        up_button = QPushButton("Up")
        up_button.clicked.connect(lambda: self._move_selected(-1))
        down_button = QPushButton("Down")
        down_button.clicked.connect(lambda: self._move_selected(1))

        row = QHBoxLayout()
        row.addWidget(self._slot_input, 1)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addWidget(up_button)
        row.addWidget(down_button)

        layout = QVBoxLayout(page)
        layout.addWidget(self._slots_list, 1)
        layout.addLayout(row)
        self._slots_list.model().rowsInserted.connect(self._update_ok)
        self._slots_list.model().rowsRemoved.connect(self._update_ok)
        return page

    def _build_dates_tab(self) -> QWidget:
        page = QWidget()
        self._calendar = QCalendarWidget()
        self._calendar.setGridVisible(True)
        add_button = QPushButton("Add selected date as option")
        add_button.clicked.connect(self._add_calendar_slot)

        layout = QVBoxLayout(page)
        layout.addWidget(self._calendar)
        layout.addWidget(add_button)
        return page

    def _add_custom_slot(self) -> None:
        text = self._slot_input.text().strip()
        if text:
            self._slots.append(text)
            self._slots_list.addItem(text)
            self._slot_input.clear()

    def _add_calendar_slot(self) -> None:
        date = self._calendar.selectedDate()
        text = date.toString(_DATE_FORMAT)
        if text not in self._slots:
            self._slots.append(text)
            self._slots_list.addItem(text)

    def _remove_selected(self) -> None:
        for item in self._slots_list.selectedItems():
            self._remove_item(item)

    def _remove_item(self, item: QListWidgetItem) -> None:
        row = self._slots_list.row(item)
        self._slots_list.takeItem(row)
        del self._slots[row]

    def _move_selected(self, delta: int) -> None:
        row = self._slots_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._slots):
            return
        item = self._slots_list.takeItem(row)
        self._slots_list.insertItem(target, item)
        self._slots_list.setCurrentRow(target)
        self._slots[row], self._slots[target] = (
            self._slots[target], self._slots[row],
        )

    def _update_ok(self, *_) -> None:
        ok = bool(self.topic()) and len(self._slots) >= 1
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)


# ---------------------------------------------------------------------------
# Convenience reads for the GUI (sync engine, GUI thread only)
# ---------------------------------------------------------------------------


def conversation_first_unread(conversation_id: int) -> int:
    return presenter.first_unread_order(conversation_id)