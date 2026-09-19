"""Project tally CRDT state into plain, GUI-ready data, free of Qt.

The GUI must not do protocol derivation itself; everything it renders is
prepared here as plain values (dataclasses / tuples / dicts) so the Qt layer
in ``katzenqt.qt_tally`` stays thin, the privsep test (no PySide6 reachable
from the ``katzenqt`` package) keeps passing, and the projection is unit
testable headlessly.

Two kinds of entry point:

* Pure: :func:`summarize`, :func:`placeholder_text` and :func:`panel_rows`
  take a pycrdt ``Doc`` (plus structural metadata) and return
  renderer-friendly data. They never touch the database or the network.
* Reading: :func:`own_voter_id`, :func:`voter_names`,
  :func:`surveys_for_conversation` and :func:`all_survey_ids` load persisted
  data through the **sync** engine (``persistent.Session(_engine_sync)``),
  the same GUI-thread-safe read path ``qt_models`` already uses. Writes stay
  on the io loop; nothing here writes.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import select

from .. import persistent
from . import engine, schema
from .engine import Outcome, SlotTally
from .schema import Mode

_UNKNOWN_VOTER = "(unknown)"


@dataclass(frozen=True)
class SurveySummary:
    """Everything the chat timeline and the poll panel need about one survey."""

    survey_id: bytes
    conversation_id: int
    conversation_order: "int | None"
    topic: str
    mode: Mode
    status: str  # "open" | "closed"
    n_slots: int
    slots: "tuple[SlotTally, ...]"
    n_voters: int
    outcome: Outcome
    creator_voter_id: "bytes | None"
    my_voter_id: "bytes | None"
    my_choices: "dict[str, str]"
    is_new: bool = False

    def voted_slot_ids(self) -> "list[str]":
        """The slot ids the local user has marked. The click-to-cycle voting
        grid seeds its state from this."""
        return list(self.my_choices)

    def my_score_on(self, slot_id: str) -> "str | None":
        """My availability on ``slot_id``, or ``None`` if I left it blank."""
        return self.my_choices.get(slot_id)

    def is_creator(self) -> bool:
        """Whether the local user opened this survey (only the creator may
        close it; peers honour closes only from the creator)."""
        return (
            self.creator_voter_id is not None
            and self.my_voter_id is not None
            and self.creator_voter_id == self.my_voter_id
        )


@dataclass(frozen=True)
class VoterRow:
    """One line of the per-voter detail view."""

    name: str
    choices: "dict[str, str]"
    has_voted: bool

    def line(self, slots: "tuple[SlotTally, ...]") -> str:
        if not self.has_voted:
            return f"{self.name}: hasn't voted"
        marks = ", ".join(
            f"{slot.text or slot.slot_id}: {self.choices[slot.slot_id]}"
            for slot in slots
            if slot.slot_id in self.choices
        )
        return f"{self.name}: {marks or 'no selections'}"


def summarize(
    doc,
    *,
    conversation_id: int,
    conversation_order: "int | None" = None,
    my_voter_id: "bytes | None" = None,
    is_new: bool = False,
) -> SurveySummary:
    """Project one survey ``Doc`` into a renderer-friendly summary. Pure."""
    result = engine.tally(doc)
    my_choices: "dict[str, str]" = {}
    if my_voter_id is not None:
        for voter in engine.per_voter(doc):
            if voter.voter_id == my_voter_id:
                my_choices = dict(voter.choices)
                break
    return SurveySummary(
        survey_id=result.survey_id,
        conversation_id=conversation_id,
        conversation_order=conversation_order,
        topic=schema.topic_of(doc),
        mode=result.mode,
        status=result.status,
        n_slots=len(result.slots),
        slots=tuple(result.slots),
        n_voters=result.n_voters,
        outcome=engine.outcome(result),
        creator_voter_id=schema.creator_of(doc),
        my_voter_id=my_voter_id,
        my_choices=my_choices,
        is_new=is_new,
    )


def placeholder_text(summary: SurveySummary) -> str:
    """The one-line chat-timeline placeholder for a survey.

    Carries the topic, the tally state and vote participation, so a survey is
    legible without opening the panel and a close is visibly reflected.
    """
    if summary.status == "closed":
        state = "closed"
    elif summary.n_voters == 0:
        state = "open · no votes yet"
    else:
        state = f"open · {summary.n_voters}/{summary.n_slots} voted"
    return f"[Poll] {summary.topic} — {state}"


def panel_rows(
    doc,
    names: "dict[bytes, str]",
) -> "tuple[VoterRow, ...]":
    """Per-voter detail rows from a survey ``Doc`` and a voter-id -> name
    mapping (see :func:`voter_names`). Pure: every recorded voter appears in
    the deterministic order the engine derives."""
    return tuple(
        VoterRow(
            name=names.get(voter.voter_id, _UNKNOWN_VOTER),
            choices=voter.choices,
            has_voted=True,
        )
        for voter in engine.per_voter(doc)
    )


def own_voter_id(conversation_id: int) -> "bytes | None":
    """Our voter identity for a conversation, from our provisioned read cap."""
    from .controller import voter_id_from_read_cap

    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, conversation_id)
        if conv is None or conv.own_peer_id is None:
            return None
        own = sess.get(persistent.ConversationPeer, conv.own_peer_id)
        if own is None:
            return None
        rcw = sess.get(persistent.ReadCapWAL, own.read_cap_id)
        if rcw is None or rcw.read_cap is None:
            return None
        return voter_id_from_read_cap(rcw.read_cap)


def voter_names(conversation_id: int) -> "dict[bytes, str]":
    """Map voter ids (read-cap hashes) to display names: every active peer
    with a provisioned read cap, plus our own peer. Unknown ids fall back to
    ``(unknown)`` at render time (callers name them ``_UNKNOWN_VOTER``)."""
    from .controller import voter_id_from_read_cap

    mapping: "dict[bytes, str]" = {}
    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, conversation_id)
        if conv is None:
            return mapping
        candidates = list(conv.peers)
        if conv.own_peer_id is not None:
            own = sess.get(persistent.ConversationPeer, conv.own_peer_id)
            if own is not None:
                candidates.append(own)
        for peer in candidates:
            if not peer.active and peer.id != conv.own_peer_id:
                continue
            rcw = sess.get(persistent.ReadCapWAL, peer.read_cap_id)
            if rcw is None or rcw.read_cap is None:
                continue
            mapping[voter_id_from_read_cap(rcw.read_cap)] = peer.name
    return mapping


def surveys_for_conversation(conversation_id: int) -> "list[tuple[bytes, int | None]]":
    """Every survey stored for a conversation, as ``(survey_id,
    conversation_order)`` pairs ordered by first-sighting position. The caller
    resolves the Doc (pycrdt load) and projects it with :func:`summarize`."""
    with persistent.Session(persistent._engine_sync) as sess:
        rows = sess.exec(
            select(persistent.TallyState)
            .where(persistent.TallyState.conversation_id == conversation_id)
            .order_by(
                persistent.TallyState.conversation_order,
                persistent.TallyState.survey_id,
            )
        ).all()
        return [(r.survey_id, r.conversation_order) for r in rows]


def survey_doc(conversation_id: int, survey_id: bytes) -> "bytes | None":
    """The persisted CRDT blob for one survey (the ``TallyState.doc_state`` the
    caller feeds to :func:`sync.load_doc`), or ``None`` if the row is gone."""
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.exec(
            select(persistent.TallyState).where(
                persistent.TallyState.conversation_id == conversation_id,
                persistent.TallyState.survey_id == survey_id,
            )
        ).first()
        return row.doc_state if row is not None else None


def all_survey_ids() -> "list[tuple[int, bytes, int | None]]":
    """``(conversation_id, survey_id, conversation_order)`` across every
    conversation, for building cross-conversation poll lists."""
    with persistent.Session(persistent._engine_sync) as sess:
        rows = sess.exec(
            select(persistent.TallyState).order_by(
                persistent.TallyState.conversation_id,
                persistent.TallyState.conversation_order,
                persistent.TallyState.survey_id,
            )
        ).all()
        return [(r.conversation_id, r.survey_id, r.conversation_order) for r in rows]


def badge_count(surveys: "list[SurveySummary]") -> int:
    """How many surveys are new (the Polls-tab badge count)."""
    return sum(1 for s in surveys if s.is_new)


def first_unread_order(conversation_id: int) -> int:
    """The persisted first-unread pointer for a conversation. The chat model
    keeps unread markers in *conversation_order* space; ``None`` (no pointer
    yet) reads as 0 so everything is initially "new", matching the chat
    view's marker semantics."""
    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, conversation_id)
        if conv is None or conv.first_unread is None:
            return 0
        return conv.first_unread


def conversation_names() -> "dict[int, str]":
    """Every conversation id -> display name, for cross-conversation poll
    lists."""
    with persistent.Session(persistent._engine_sync) as sess:
        rows = sess.exec(select(persistent.Conversation)).all()
        return {r.id: r.name for r in rows}