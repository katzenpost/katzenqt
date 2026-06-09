"""The survey data model: how a tally lives inside a pycrdt ``Doc``.

One ``Doc`` holds one survey. Three root types carry the whole of it:

* ``meta`` (Map): ``survey_id`` (hex), ``topic``, ``mode``, ``n_slots``,
  ``status`` (``"open"`` or ``"closed"``),
* ``slots`` (Array of Maps ``{id, text}``): fixed at creation,
* ``votes`` (Map): voter id (hex) -> a Map of ``slot_id -> availability``.

Counts are never stored; they are derived by :func:`katzenqt.tally.engine.tally`.

Root values are read back through ``doc.get(name, type=...)`` so that a ``Doc``
rebuilt from a received update (where the roots are materialised by the update
rather than declared up front) reads identically to a freshly created one.
"""
from __future__ import annotations

from enum import Enum

from pycrdt import Array, Doc, Map

_META = "meta"
_SLOTS = "slots"
_VOTES = "votes"

# A reserved key inside a voter's choice Map carrying the vote's version. It is
# never a slot id, so the tally derivation skips it. The version exists for a
# later "honour my most recent intent" override; the engine does not enforce it
# yet (that is the hardening phase).
_VERSION_KEY = "_version"


class Mode(Enum):
    """The two voting modes, unified as a per-slot availability map.

    ``APPROVAL`` is the degenerate availability domain ``{yes, no}`` (approve any
    number of slots); ``AVAILABILITY`` widens it with ``maybe`` (Doodle-style).
    """

    AVAILABILITY = "availability"
    APPROVAL = "approval"


_DOMAINS: "dict[Mode, tuple[str, ...]]" = {
    Mode.AVAILABILITY: ("yes", "maybe", "no"),
    Mode.APPROVAL: ("yes", "no"),
}


def domain(mode: Mode) -> "tuple[str, ...]":
    """The availabilities a vote may use in ``mode``."""
    return _DOMAINS[mode]


def slot_id(index: int) -> str:
    """The stable slot id for the slot at ``index`` (``s0``, ``s1``, ...)."""
    return f"s{index}"


def new_survey_doc(survey_id: bytes, topic: str, mode: Mode, slots: "list[str]") -> Doc:
    """Build a fresh survey ``Doc``. ``slots`` is the list of descriptive texts,
    one per slot; slot ids are assigned by position."""
    if not slots:
        raise ValueError("a survey needs at least one slot")
    doc = Doc()
    doc[_META] = Map()
    doc[_SLOTS] = Array()
    doc[_VOTES] = Map()
    with doc.transaction():
        meta = doc.get(_META, type=Map)
        meta["survey_id"] = survey_id.hex()
        meta["topic"] = topic
        meta["mode"] = mode.value
        meta["n_slots"] = len(slots)
        meta["status"] = "open"
        arr = doc.get(_SLOTS, type=Array)
        for i, text in enumerate(slots):
            arr.append(Map({"id": slot_id(i), "text": text}))
    return doc


def meta_map(doc: Doc) -> Map:
    return doc.get(_META, type=Map)


def votes_map(doc: Doc) -> Map:
    return doc.get(_VOTES, type=Map)


def mode_of(doc: Doc) -> Mode:
    return Mode(meta_map(doc)["mode"])


def status_of(doc: Doc) -> str:
    return meta_map(doc)["status"]


def survey_id_of(doc: Doc) -> bytes:
    return bytes.fromhex(meta_map(doc)["survey_id"])


def topic_of(doc: Doc) -> str:
    return meta_map(doc)["topic"]


def slots_of(doc: Doc) -> "list[tuple[str, str]]":
    """The survey's slots as ``(slot_id, text)`` pairs, in creation order."""
    arr = doc.get(_SLOTS, type=Array)
    return [(arr[i]["id"], arr[i]["text"]) for i in range(len(arr))]
