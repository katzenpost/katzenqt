"""The network's epoch period, as the PKI last reported it.

Every wait that has to ride out a PKI rollover is one epoch plus headroom.
The period is recovered from the PKI document at runtime, so pointing a
client at a network whose epoch is twenty minutes instead of two needs no
configuration and no edit.
"""

import os

_period_s: "float | None" = None


def remember_period(period_s_: "int | float | None") -> None:
    """Record the epoch period the current PKI epoch implies."""
    global _period_s
    if period_s_ is not None and period_s_ > 0:
        _period_s = float(period_s_)


def forget_period() -> None:
    """Drop the remembered period, for tests and for a fresh client."""
    global _period_s
    _period_s = None


def period_s() -> float:
    """The epoch period in seconds, 0.0 while nothing has said what it is.

    KQT_EPOCH_DURATION_S answers for a network the PKI has not described
    yet, which is how the integration harness points at a live mixnet
    before the first document arrives.

    >>> period_s() >= 0.0
    True
    """
    if _period_s is not None:
        return _period_s
    override = os.environ.get("KQT_EPOCH_DURATION_S")
    if override:
        try:
            value = float(override)
        except ValueError:
            return 0.0
        if value > 0.0:
            return value
    return 0.0


def budget_s(headroom_s: float) -> float:
    """One epoch plus ``headroom_s``.

    >>> budget_s(180.0) >= 180.0
    True
    """
    return period_s() + headroom_s
