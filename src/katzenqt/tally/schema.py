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
from typing import cast

from pycrdt import Array, Doc, Map
from pycrdt._base import BaseType

SurveyDoc = Doc[BaseType]

MetaMap = Map[str | float]
ChoiceMap = Map[str | float]
VotesMap = Map[ChoiceMap]
SlotMap = Map[str]
SlotsArray = Array[SlotMap]

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
    """The availabilities a vote may use in ``mode``.

    >>> domain(Mode.APPROVAL)
    ('yes', 'no')
    >>> domain(Mode.AVAILABILITY)
    ('yes', 'maybe', 'no')
    """
    return _DOMAINS[mode]


def slot_id(index: int) -> str:
    """The stable slot id for the slot at ``index`` (``s0``, ``s1``, ...).

    >>> [slot_id(i) for i in (0, 1, 10)]
    ['s0', 's1', 's10']
    """
    return f"s{index}"


def new_survey_doc(survey_id: bytes, topic: str, mode: Mode, slots: "list[str] | tuple[str, ...]",
                   creator: "bytes | None" = None) -> SurveyDoc:
    """Build a fresh survey ``Doc``. ``slots`` is the list of descriptive texts,
    one per slot; slot ids are assigned by position. ``creator`` is the voter id
    of whoever opened the survey; it is recorded so peers can verify that a close
    came from the creator.

    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon", "Tue"])
    >>> slots_of(doc)
    [('s0', 'Mon'), ('s1', 'Tue')]
    >>> status_of(doc), topic_of(doc)
    ('open', 'Lunch')
    >>> new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, [])
    Traceback (most recent call last):
        ...
    ValueError: a survey needs at least one slot
    """
    if not slots:
        raise ValueError("a survey needs at least one slot")
    doc: SurveyDoc = Doc()
    doc[_META] = Map()
    doc[_SLOTS] = Array()
    doc[_VOTES] = Map()
    with doc.transaction():
        meta = meta_map(doc)
        meta["survey_id"] = survey_id.hex()
        meta["topic"] = topic
        meta["mode"] = mode.value
        meta["n_slots"] = len(slots)
        meta["status"] = "open"
        if creator is not None:
            meta["creator"] = creator.hex()
        arr = slots_array(doc)
        for i, text in enumerate(slots):
            arr.append(Map({"id": slot_id(i), "text": text}))
    return doc


def meta_map(doc: SurveyDoc) -> MetaMap:
    """The survey's ``meta`` root map.

    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> sorted(meta_map(doc).keys())
    ['mode', 'n_slots', 'status', 'survey_id', 'topic']
    >>> meta_map(doc)["mode"]
    'approval'
    >>> meta_map(doc)["n_slots"]
    1.0
    """
    root = doc.get(_META, type=Map)
    assert isinstance(root, Map)
    return root


def votes_map(doc: SurveyDoc) -> VotesMap:
    """The survey's ``votes`` root map, keyed by hex voter id.

    >>> from katzenqt.tally.engine import apply_vote
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> list(votes_map(doc).keys())
    []
    >>> apply_vote(doc, bytes([1, 2]), {"s0": "yes"})
    >>> list(votes_map(doc).keys())
    ['0102']
    """
    root = doc.get(_VOTES, type=Map)
    assert isinstance(root, Map)
    return root


def slots_array(doc: SurveyDoc) -> SlotsArray:
    """The survey's ``slots`` root array, in creation order.

    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon", "Tue"])
    >>> len(slots_array(doc))
    2
    >>> slots_array(doc)[1]["id"], slots_array(doc)[1]["text"]
    ('s1', 'Tue')
    """
    root = doc.get(_SLOTS, type=Array)
    assert isinstance(root, Array)
    return root


def mode_of(doc: SurveyDoc) -> Mode:
    """The survey's voting mode, decoded back into :class:`Mode`.

    >>> mode_of(new_survey_doc(bytes(16), "L", Mode.APPROVAL, ["Mon"]))
    <Mode.APPROVAL: 'approval'>
    >>> mode_of(new_survey_doc(bytes(16), "L", Mode.AVAILABILITY, ["Mon"]))
    <Mode.AVAILABILITY: 'availability'>
    """
    return Mode(meta_map(doc)["mode"])


def status_of(doc: SurveyDoc) -> str:
    """Whether the survey is still open.

    >>> from katzenqt.tally.engine import close_survey
    >>> doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    >>> status_of(doc)
    'open'
    >>> close_survey(doc)
    >>> status_of(doc)
    'closed'
    """
    return cast(str, meta_map(doc)["status"])


def survey_id_of(doc: SurveyDoc) -> bytes:
    """The survey id, decoded from the hex stored in ``meta``.

    >>> survey = bytes.fromhex("00112233445566778899aabbccddeeff")
    >>> survey_id_of(
    ...     new_survey_doc(survey, "L", Mode.APPROVAL, ["Mon"])) == survey
    True
    >>> survey_id_of(new_survey_doc(survey, "L", Mode.APPROVAL, ["Mon"])).hex()
    '00112233445566778899aabbccddeeff'
    """
    return bytes.fromhex(cast(str, meta_map(doc)["survey_id"]))


def topic_of(doc: SurveyDoc) -> str:
    """The survey's topic line, stored verbatim.

    >>> topic_of(new_survey_doc(bytes(16), "Lunch: Monday?", Mode.APPROVAL, ["a"]))
    'Lunch: Monday?'
    """
    return cast(str, meta_map(doc)["topic"])


def creator_of(doc: SurveyDoc) -> "bytes | None":
    """The voter id that opened the survey, or ``None`` for surveys created
    before the creator was recorded.

    >>> creator_of(new_survey_doc(bytes(16), "L", Mode.APPROVAL, ["Mon"])) is None
    True
    >>> creator_of(new_survey_doc(
    ...     bytes(16), "L", Mode.APPROVAL, ["Mon"], creator=bytes(range(16)),
    ... )).hex()
    '000102030405060708090a0b0c0d0e0f'
    """
    meta = meta_map(doc)
    if "creator" in set(meta.keys()):
        return bytes.fromhex(cast(str, meta["creator"]))
    return None


def slots_of(doc: SurveyDoc) -> "list[tuple[str, str]]":
    """The survey's slots as ``(slot_id, text)`` pairs, in creation order.

    >>> slots_of(new_survey_doc(bytes(16), "L", Mode.APPROVAL, ["Tue", "Mon"]))
    [('s0', 'Tue'), ('s1', 'Mon')]
    """
    arr = slots_array(doc)
    return [(arr[i]["id"], arr[i]["text"]) for i in range(len(arr))]
