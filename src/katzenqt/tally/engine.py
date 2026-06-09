"""Local mutations and the pure tally derivation.

A vote is written into the ``Doc`` under the voter's own key; the tally is a
pure function of the ``votes`` map and the ``slots`` list. No counts are stored.
"""
from __future__ import annotations

from dataclasses import dataclass

from pycrdt import Doc, Map

from .schema import (
    _VERSION_KEY,
    Mode,
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


def apply_vote(doc: Doc, voter_id: bytes, choice: "dict[str, str]", version: int = 0) -> None:
    """Record ``voter_id``'s ``choice`` (a ``slot_id -> availability`` map).

    Every slot id must exist and every availability must lie in the mode's
    domain, else :class:`ValueError`. A re-vote overwrites the voter's prior
    choice. Only the voter's own key is touched.
    """
    allowed = domain(mode_of(doc))
    valid_slots = {sid for sid, _ in slots_of(doc)}
    for sid, avail in choice.items():
        if sid not in valid_slots:
            raise ValueError(f"unknown slot id {sid!r}")
        if avail not in allowed:
            raise ValueError(f"availability {avail!r} not allowed in {mode_of(doc).value} mode")
    payload = dict(choice)
    payload[_VERSION_KEY] = version
    with doc.transaction():
        votes_map(doc)[voter_id.hex()] = Map(payload)


def close_survey(doc: Doc) -> None:
    """Mark the survey closed. Peers honour this locally."""
    with doc.transaction():
        meta_map(doc)["status"] = "closed"


def tally(doc: Doc) -> TallyResult:
    """Derive the per-slot counts. Pure: it reads the ``Doc`` and stores nothing.

    A voter who omitted a slot counts as ``no`` for that slot.
    """
    votes = votes_map(doc)
    choices = []
    for voter in votes.keys():
        vmap = votes[voter]
        choices.append({k: vmap[k] for k in vmap.keys() if k != _VERSION_KEY})

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
