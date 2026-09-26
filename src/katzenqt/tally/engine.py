"""Local mutations and the pure tally derivation.

A vote is written into the ``Doc`` under the voter's own key; the tally is a
pure function of the ``votes`` map and the ``slots`` list. No counts are stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pycrdt import Doc, Map

from .schema import (
    _VERSION_KEY,
    Mode,
    SurveyDoc,
    VotesMap,
    domain,
    meta_map,
    mode_of,
    slots_of,
    status_of,
    survey_id_of,
    votes_map,
)


@dataclass(frozen=True)
class SlotTally:
    """Per-slot counts. ``maybe`` is always ``0`` in approval mode."""

    slot_id: str
    text: str
    yes: int
    maybe: int
    no: int


@dataclass(frozen=True)
class TallyResult:
    survey_id: bytes
    mode: Mode
    status: str
    n_voters: int
    slots: "list[SlotTally]"


@dataclass(frozen=True)
class VoterChoice:
    """One voter's recorded vote: the availability they marked on each slot
    (a slot they left unmarked is omitted, so an all-no vote is ``{}``), plus
    the version used to order recasts."""

    voter_id: bytes
    version: int
    choices: "dict[str, str]"


def _stored_version(votes: VotesMap, key: str) -> int:
    """The version of the vote under ``key``, or ``-1`` if there is none.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> _stored_version(votes_map(doc), "0102")
    -1
    >>> apply_vote(doc, bytes([1, 2]), {"s0": "yes"}, version=4)
    >>> int(_stored_version(votes_map(doc), "0102"))
    4
    """
    if key not in set(votes.keys()):
        return -1
    existing = votes[key]
    return cast(int, existing[_VERSION_KEY]) if _VERSION_KEY in set(existing.keys()) else 0


def current_version(doc: SurveyDoc, voter_id: bytes) -> int:
    """The version of ``voter_id``'s recorded vote, or ``-1`` if they have not
    yet voted. Callers bump this to mint the next version on a recast.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> current_version(doc, b"voter-1")
    -1
    >>> apply_vote(doc, b"voter-1", {"s0": "yes"}, version=3)
    >>> int(current_version(doc, b"voter-1"))
    3
    >>> current_version(doc, b"voter-2")
    -1
    """
    return _stored_version(votes_map(doc), voter_id.hex())


def stored_choice(doc: SurveyDoc, voter_id: bytes) -> "tuple[int, dict[str, str]] | None":
    """The ``(version, choices)`` recorded for ``voter_id``, or ``None`` if they
    have not voted. Lets a replay tell whether the ballot it holds is already
    what the Doc records, without mutating it.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon", "Tue"])
    >>> stored_choice(doc, b"voter-1") is None
    True
    >>> apply_vote(doc, b"voter-1", {"s0": "yes"}, version=2)
    >>> apply_vote(doc, b"voter-1", {"s1": "yes"}, version=1)
    >>> version, choices = stored_choice(doc, b"voter-1")
    >>> int(version), choices
    (2, {'s0': 'yes'})
    """
    votes = votes_map(doc)
    key = voter_id.hex()
    if key not in set(votes.keys()):
        return None
    vmap = votes[key]
    choice = {k: cast(str, vmap[k]) for k in vmap.keys() if k != _VERSION_KEY}
    version = cast(int, vmap[_VERSION_KEY]) if _VERSION_KEY in set(vmap.keys()) else 0
    return version, choice


def apply_vote(doc: SurveyDoc, voter_id: bytes, choice: "dict[str, str]", version: int = 0) -> None:
    """Record ``voter_id``'s ``choice`` (a ``slot_id -> availability`` map).

    Every slot id must exist and every availability must lie in the mode's
    domain, else :class:`ValueError`. The version carries the voter's intent
    order: a newer version supersedes their prior vote, and an older one is
    discarded, so the latest version wins whatever the arrival order. Only the
    voter's own key is ever touched.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> apply_vote(doc, b"voter-1", {"s9": "yes"})
    Traceback (most recent call last):
        ...
    ValueError: unknown slot id 's9'
    >>> apply_vote(doc, b"voter-1", {"s0": "maybe"})
    Traceback (most recent call last):
        ...
    ValueError: availability 'maybe' not allowed in approval mode
    >>> apply_vote(doc, b"voter-1", {"s0": "yes"}, version=5)
    >>> apply_vote(doc, b"voter-1", {}, version=4)
    >>> stored_choice(doc, b"voter-1")[1]
    {'s0': 'yes'}
    """
    allowed = domain(mode_of(doc))
    valid_slots = {sid for sid, _ in slots_of(doc)}
    for sid, avail in choice.items():
        if sid not in valid_slots:
            raise ValueError(f"unknown slot id {sid!r}")
        if avail not in allowed:
            raise ValueError(f"availability {avail!r} not allowed in {mode_of(doc).value} mode")
    key = voter_id.hex()
    votes = votes_map(doc)
    with doc.transaction():
        if version < _stored_version(votes, key):
            return  # a newer version is already recorded; keep it
        payload: "dict[str, str | float]" = dict(choice)
        payload[_VERSION_KEY] = version
        votes[key] = Map(payload)


def close_survey(doc: SurveyDoc) -> None:
    """Mark the survey closed. Peers honour this locally.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> status_of(doc)
    'open'
    >>> close_survey(doc)
    >>> tally(doc).status
    'closed'
    """
    with doc.transaction():
        meta_map(doc)["status"] = "closed"


def per_voter(doc: SurveyDoc) -> "list[VoterChoice]":
    """The per-voter detail view: each recorded vote keyed to its voter id,
    with the slot availability map and the vote's version. Pure: reads the
    ``Doc`` and stores nothing. Voter order is the sorted hex keys, so two
    peers derive the same list. The voter id is the payload-independent
    identity (see ``controller.voter_id_from_read_cap``).

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> apply_vote(doc, bytes([2]), {"s0": "yes"})
    >>> apply_vote(doc, bytes([1]), {})
    >>> [v.voter_id.hex() for v in per_voter(doc)]
    ['01', '02']
    >>> per_voter(doc)[0].choices, per_voter(doc)[1].choices
    ({}, {'s0': 'yes'})
    """
    votes = votes_map(doc)
    out = []
    for voter in sorted(votes.keys()):
        vmap = votes[voter]
        choice = {k: cast(str, vmap[k]) for k in vmap.keys() if k != _VERSION_KEY}
        version = cast(int, vmap[_VERSION_KEY]) if _VERSION_KEY in set(vmap.keys()) else 0
        out.append(VoterChoice(
            voter_id=bytes.fromhex(voter), version=version, choices=choice,
        ))
    return out


def tally(doc: SurveyDoc) -> TallyResult:
    """Derive the per-slot counts. Pure: it reads the ``Doc`` and stores nothing.

    A voter who omitted a slot counts as ``no`` for that slot.

    >>> from katzenqt.tally.schema import new_survey_doc
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon", "Tue"])
    >>> apply_vote(doc, b"a", {"s0": "yes"})
    >>> apply_vote(doc, b"b", {})
    >>> result = tally(doc)
    >>> result.n_voters
    2
    >>> [(s.slot_id, s.yes, s.no) for s in result.slots]
    [('s0', 1, 1), ('s1', 0, 2)]
    """
    choices = [v.choices for v in per_voter(doc)]

    slots = []
    for sid, text in slots_of(doc):
        yes = maybe = no = 0
        for choice in choices:
            avail = choice.get(sid, "no")
            if avail == "yes":
                yes += 1
            elif avail == "maybe":
                maybe += 1
            else:
                no += 1
        slots.append(SlotTally(slot_id=sid, text=text, yes=yes, maybe=maybe, no=no))

    return TallyResult(
        survey_id=survey_id_of(doc),
        mode=mode_of(doc),
        status=status_of(doc),
        n_voters=len(choices),
        slots=slots,
    )


@dataclass(frozen=True)
class Outcome:
    """The declared result of a tally. ``kind`` is ``"winner"`` (one slot has
    the most yes votes), ``"tie"`` (several share the top), or ``"no_winner"``
    (no slot received a yes). ``winners`` is the slot(s) at the top, empty for
    ``no_winner``; ``top_yes`` is their yes count."""

    kind: str
    winners: "list[SlotTally]"
    top_yes: int


def outcome(result: TallyResult) -> Outcome:
    """Declare the winner of a tally, ties and all, by yes count. Pure.

    >>> mon = SlotTally("s0", "Mon", yes=2, maybe=0, no=0)
    >>> tue = SlotTally("s1", "Tue", yes=2, maybe=0, no=0)
    >>> wed = SlotTally("s2", "Wed", yes=1, maybe=1, no=0)
    >>> tied = outcome(TallyResult(bytes(16), Mode.APPROVAL, "open", 2,
    ...                            [mon, tue, wed]))
    >>> tied.kind, tied.top_yes, [s.slot_id for s in tied.winners]
    ('tie', 2, ['s0', 's1'])
    >>> won = outcome(TallyResult(bytes(16), Mode.APPROVAL, "open", 2,
    ...                           [mon, wed]))
    >>> won.kind, [s.slot_id for s in won.winners]
    ('winner', ['s0'])
    >>> outcome(TallyResult(bytes(16), Mode.APPROVAL, "open", 0, []))
    Outcome(kind='no_winner', winners=[], top_yes=0)
    """
    top_yes = max((s.yes for s in result.slots), default=0)
    if top_yes == 0:
        return Outcome(kind="no_winner", winners=[], top_yes=0)
    winners = [s for s in result.slots if s.yes == top_yes]
    kind = "tie" if len(winners) > 1 else "winner"
    return Outcome(kind=kind, winners=winners, top_yes=top_yes)
__all__ = [
    "Doc",
    "Map",
    "Mode",
    "Outcome",
    "SlotTally",
    "SurveyDoc",
    "TallyResult",
    "VoterChoice",
    "VotesMap",
    "annotations",
    "apply_vote",
    "close_survey",
    "current_version",
    "dataclass",
    "domain",
    "meta_map",
    "mode_of",
    "outcome",
    "per_voter",
    "slots_of",
    "status_of",
    "stored_choice",
    "survey_id_of",
    "tally",
    "votes_map",
]

