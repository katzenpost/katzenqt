from __future__ import annotations

import math
from typing import cast

import pytest
from pycrdt import Doc, Map

from katzenqt.tally import engine
from katzenqt.tally.schema import (
    _VERSION_KEY,
    Mode,
    new_survey_doc,
    votes_map,
)


def _vmap(payload: "dict[str, object]") -> "Map[str | float]":
    doc: "Doc[Map[str | float]]" = Doc()
    vmap: "Map[str | float]" = Map(
        cast("dict[str, str | float]", payload),
    )
    doc["vote"] = vmap
    return vmap


def test_a_vote_without_a_version_is_the_first_vote() -> None:
    assert engine._version_of(_vmap({"s0": "yes"})) == 0


def test_a_numeric_version_is_kept() -> None:
    assert engine._version_of(_vmap({_VERSION_KEY: 3.0})) == 3


@pytest.mark.parametrize("version", ["7", True, None, math.nan, math.inf])
def test_an_unusable_version_is_treated_as_absent(version: object) -> None:
    assert engine._version_of(_vmap({_VERSION_KEY: version})) == 0


def test_one_malformed_ballot_does_not_deny_the_survey() -> None:
    doc = new_survey_doc(bytes(16), "Lunch", Mode.APPROVAL, ["Mon"])
    engine.apply_vote(doc, b"good", {"s0": "yes"}, version=2)
    votes_map(doc)[b"bad".hex()] = Map(
        cast(
            "dict[str, str | float]",
            {"s0": "yes", _VERSION_KEY: "not a number"},
        ),
    )

    assert engine.tally(doc).slots[0].yes == 2
    assert [v.version for v in engine.per_voter(doc)] == [0, 2]
    assert engine.stored_choice(doc, b"bad") == (0, {"s0": "yes"})
    assert engine.current_version(doc, b"bad") == 0

    engine.apply_vote(doc, b"bad", {"s0": "no"}, version=1)
    assert engine.stored_choice(doc, b"bad") == (1, {"s0": "no"})
