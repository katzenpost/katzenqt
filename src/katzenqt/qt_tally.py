"""Qt-only models and widgets for the tally/poll GUI.

Everything here imports PySide6, so nothing in ``katzenqt``, ``persistent``,
``network`` or ``models`` may import it (see tests/test_qt_decoupling.py); the
tally protocol derivation stays in ``katzenqt.tally.presenter`` / ``engine``,
which this module projects into Qt models and widgets.

Two pieces:

* :class:`TallyPanel`: a modeless poll window (one instance per open poll)
  with a grid of every voter's ballot and click-to-cycle editing for the
  local user.
* :class:`TallyCreateDialog`: the hybrid create dialog (custom options +
  date picks).

Tally messages themselves are ordinary chat rows; their display is derived in
:mod:`katzenqt.qt_models` via :mod:`katzenqt.tally.presenter`.

The panel's voting grid is deliberately dumb: it edits a local selection map
and emits it as a plain ``dict`` on :attr:`TallyPanel.voteSubmitted`; wiring
(io-loop persistence through ``katzenqt.tally.controller``) is the caller's
job, in the same `run_in_io` style the chat composer uses.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
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


class TallyPanel(QDialog):
    """Modeless poll window: header, a grid of every voter's ballot, the local
    user's click-to-cycle editing, and (for the creator) an End button.

    One instance per open poll, created and shown by the MainWindow; the window
    title names the conversation the poll belongs to. The grid edits a local
    selection and emits it on :attr:`voteSubmitted` as
    ``{slot_id: availability}`` for the caller to persist on the io loop. It
    never writes the database itself."""

    voteSubmitted = Signal(dict)  # slot_id -> availability (only set slots)
    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Poll")
        self._conversation_label = ""
        self._summary: "SurveySummary | None" = None
        self._survey_key: "tuple[int, bytes] | None" = None
        self._voters: "tuple[presenter.VoterRow, ...]" = ()
        self._submit_base: "dict[str, str]" = {}
        self._selection: "dict[str, str]" = {}
        self._editing = False
        self._sized = False
        self._cycle: "list[str]" = []  # availabilities to cycle (no blank)
        self._slot_text: "dict[str, str]" = {}
        self._slot_buttons: "dict[str, QToolButton]" = {}
        self._edit_button: "QPushButton | None" = None

        self._topic_label = QLabel("No poll selected")
        self._topic_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self._status_label = QLabel("")
        self._meta_label = QLabel("")
        self._outcome_label = QLabel("")

        # The grid lives in a scroll area so a large poll scrolls instead of
        # being squeezed (which used to clip the first or last row).
        self._grid = QGridLayout()
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(4)
        self._grid_host = QWidget()
        self._grid_host.setLayout(self._grid)
        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidget(self._grid_host)
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._vote_button = QPushButton("Send vote")
        self._vote_button.setEnabled(False)
        self._vote_button.clicked.connect(self._submit)
        self._edit_button = QPushButton("Edit vote")
        self._edit_button.clicked.connect(self._start_editing)
        self._edit_button.hide()
        self._close_button = QPushButton("End poll")
        self._close_button.hide()
        self._close_button.clicked.connect(self.closeRequested)

        buttons = QHBoxLayout()
        buttons.addWidget(self._vote_button)
        buttons.addWidget(self._edit_button)
        buttons.addStretch(1)
        buttons.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._topic_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._meta_label)
        layout.addWidget(self._outcome_label)
        layout.addWidget(self._grid_scroll, 1)
        layout.addLayout(buttons)

    # -- population ----------------------------------------------------------

    def set_conversation_label(self, label: str) -> None:
        """Name the conversation this poll belongs to (shown in the window
        title)."""
        self._conversation_label = label
        self._update_window_title()

    def _update_window_title(self) -> None:
        prefix = self._conversation_label or "Poll"
        topic = self._summary.topic if self._summary is not None else None
        self.setWindowTitle(f"{prefix} — Poll: {topic}" if topic else f"{prefix} — Poll")

    def set_survey(
        self,
        summary: SurveySummary,
        voters: "tuple[presenter.VoterRow, ...]",
        *,
        preserve_edits: bool = False,
    ) -> None:
        """Render a survey and its per-voter rows.

        Unless ``preserve_edits``, the local edit buffer resets to the persisted
        ballot and editing starts only when the local user has not voted yet.
        ``preserve_edits`` keeps an in-progress edit across a refresh of the
        same survey."""
        self._summary = summary
        self._voters = voters
        self._cycle = list(schema.domain(summary.mode))
        self._submit_base = dict(summary.my_choices)
        if preserve_edits and summary.status == "open":
            pass  # keep the in-progress selection and edit mode
        else:
            self._selection = dict(summary.my_choices)
            me = next(
                (v for v in voters if v.voter_id == summary.my_voter_id), None,
            )
            self._editing = (
                summary.status == "open"
                and (me is None or not me.has_voted)
            )

        self._set_header(summary)
        self._rebuild_grid()
        self._update_vote_enabled()
        self._update_window_title()
        is_open = summary.status == "open"
        self._close_button.setVisible(is_open and summary.is_creator())

    def show_survey(self, conversation_id: int, survey_id: bytes) -> bool:
        """Load one survey from persisted state and render it (header and
        voter grid). Returns False when the survey is unknown; otherwise
        records it as the current survey so a later refresh (a received vote or
        close) can re-render the same one. An in-progress local edit survives a
        refresh of the same survey."""
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
        voters = presenter.panel_rows(
            doc, names, my_voter_id=summary.my_voter_id,
        )
        same = self._survey_key == (conversation_id, survey_id)
        self._survey_key = (conversation_id, survey_id)
        self.set_survey(summary, voters, preserve_edits=same and self._editing)
        if not self.isVisible():
            # First open (or reopened): size to the content rather than the
            # default. A refresh of an open window never resizes it.
            self.adjustSize()
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
        self._voters = ()
        self._submit_base = {}
        self._selection = {}
        self._editing = False
        self._cycle = []
        self._clear_grid()
        self._topic_label.setText("No poll selected")
        self._status_label.setText("")
        self._meta_label.setText("")
        self._outcome_label.setText("")
        self._vote_button.setEnabled(False)
        self._edit_button.hide()
        self._close_button.hide()
        self._update_window_title()

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

    @staticmethod
    def _slot_totals(slot) -> str:
        totals = f"yes {slot.yes}"
        if slot.maybe:
            totals += f" · maybe {slot.maybe}"
        totals += f" · no {slot.no}"
        return totals

    def _rebuild_grid(self) -> None:
        """Draw the header row (one column per option) and one row per voter.

        The name column is right-aligned and sized to its content; the option
        columns share the remaining width and centre their headings and cells.
        The local user's row is click-to-cycle while editing, read-only with the
        Edit button once they have voted."""
        self._clear_grid()
        summary = self._summary
        if summary is None:
            return
        slots = summary.slots

        voter_header = QLabel("Voter")
        voter_header.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._grid.addWidget(voter_header, 0, 0)
        for col, slot in enumerate(slots, start=1):
            self._slot_text[slot.slot_id] = slot.text or slot.slot_id
            header = QLabel(self._slot_text[slot.slot_id])
            header.setAlignment(Qt.AlignCenter)
            header.setToolTip(self._slot_totals(slot))
            self._grid.addWidget(header, 0, col, Qt.AlignCenter)

        is_open = summary.status == "open"
        me = next(
            (
                v for v in self._voters
                if summary.my_voter_id is not None
                and v.voter_id == summary.my_voter_id
            ),
            None,
        )
        for row, voter in enumerate(self._voters, start=1):
            is_me = voter is me
            name = QLabel(voter.name)
            name.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._grid.addWidget(name, row, 0)
            editable = is_me and self._editing and is_open
            for col, slot in enumerate(slots, start=1):
                if editable:
                    button = QToolButton()
                    button.setToolTip(self._slot_totals(slot))
                    self._slot_buttons[slot.slot_id] = button
                    self._refresh_button(slot.slot_id)
                    button.clicked.connect(
                        lambda _=False, sid=slot.slot_id: self._cycle_slot(sid)
                    )
                    self._grid.addWidget(button, row, col, Qt.AlignCenter)
                else:
                    avail = (
                        self._selection.get(slot.slot_id)
                        if is_me
                        else voter.choices.get(slot.slot_id)
                    )
                    cell = QLabel(avail or "")
                    cell.setAlignment(Qt.AlignCenter)
                    self._grid.addWidget(cell, row, col, Qt.AlignCenter)

        # The name column keeps its natural width; the option columns share the
        # rest equally so the grid fills the window with no gap.
        for col in range(len(slots) + 1):
            self._grid.setColumnStretch(col, 0 if col == 0 else 1)
        self._edit_button.setVisible(
            me is not None and me.has_voted and not self._editing and is_open
        )
        self._apply_size_constraints()
        # A QGridLayout does not refresh its size hint until the pending layout
        # request is processed, so re-fit once the event loop has run; reading
        # the hint synchronously would use the pre-edit size and miss the
        # growth when Edit vote swaps cells for buttons.
        QTimer.singleShot(0, self._apply_size_constraints)

    def _apply_size_constraints(self) -> None:
        """Size the window to the grid's content, never larger than the screen.

        A QScrollArea does not advertise the full size of the widget it hosts,
        so ``self.sizeHint()`` does not grow when Edit vote swaps the read-only
        cells for larger buttons. Build the preferred size from the grid host's
        own hint plus the surrounding chrome; the scroll area takes any
        overflow, so the rows are never squeezed to fit (which clipped the
        first or last row). Growing only: setMinimumSize enlarges a visible
        window when the content grows but never shrinks one when it shrinks."""
        self.layout().activate()
        chrome = self.layout().sizeHint() - self._grid_scroll.sizeHint()
        preferred = self._grid_host.sizeHint() + chrome
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry().size()
            avail.setHeight(max(240, avail.height() - 80))  # title bar + margin
            avail.setWidth(max(240, avail.width() - 40))
            self.setMaximumSize(avail)
            preferred = preferred.boundedTo(avail)
        self.setMinimumSize(preferred)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._sized:
            # The pre-show size hint can be a hair short; re-fit once the
            # widget is polished so the first paint is not clipped.
            self._sized = True
            self._apply_size_constraints()
            self.adjustSize()

    def _refresh_button(self, slot_id: str) -> None:
        avail = self._selection.get(slot_id)
        label = self._slot_text.get(slot_id, slot_id)
        self._slot_buttons[slot_id].setText(
            label if avail is None else f"{label}: {avail}"
        )

    # -- interaction ------------------------------------------------------------

    def _start_editing(self) -> None:
        """Reveal the local row's toggles, seeded from the current ballot, so
        the user can change their vote and send it."""
        if self._summary is None or self._summary.status != "open":
            return
        self._editing = True
        self._rebuild_grid()
        self._update_vote_enabled()

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
        if not self._vote_button.isEnabled():
            return
        self._submit_base = dict(self._selection)
        self._editing = False
        self.voteSubmitted.emit(dict(self._selection))
        self._rebuild_grid()
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