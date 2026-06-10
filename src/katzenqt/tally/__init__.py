"""The tally protocol: a decentralised survey whose convergent state is a
pycrdt ``Doc``.

This package is the pure, self-contained heart of the feature. It holds no
Qt and no networking: the data model (:mod:`schema`), the local mutations and
the pure derivation (:mod:`engine`), and the thin CRDT sync helpers
(:mod:`sync`). It is verifiable in full with two in-process ``Doc`` objects
and no transport, which is precisely how its tests exercise it.

The transport carriers and the controller that drives this protocol over the
group chat live elsewhere (``katzenqt.models``, ``katzenqt.tally.controller``,
``katzenqt.tally.send``) so that importing the protocol core stays cheap.
"""
from __future__ import annotations

from .engine import (
    Outcome,
    SlotTally,
    TallyResult,
    apply_vote,
    close_survey,
    current_version,
    outcome,
    tally,
)
from .schema import Mode, domain, new_survey_doc, slot_id, slots_of

__all__ = [
    "Mode",
    "Outcome",
    "SlotTally",
    "TallyResult",
    "apply_vote",
    "close_survey",
    "current_version",
    "domain",
    "new_survey_doc",
    "outcome",
    "slot_id",
    "slots_of",
    "tally",
]
