"""The network's epoch period, as the PKI last reported it.

Every wait that has to ride out a PKI rollover is one epoch plus headroom.
The period is recovered from the PKI document at runtime, so pointing a
client at a network whose epoch is twenty minutes instead of two needs no
configuration and no edit.
"""


def period_s(
    derived_s: "float | None", override: "str | None" = None,
) -> float:
    """The epoch period in seconds, 0.0 while nothing has said what it is.

    ``derived_s`` is what the PKI document implies. ``override`` answers
    for a network the PKI has not described yet, which is how the
    integration harness points at a live mixnet before the first document
    arrives; it is the raw string, so no caller has to parse it.

    >>> period_s(120.0)
    120.0
    >>> period_s(None, "1200")
    1200.0
    >>> period_s(None, "soon")
    0.0
    >>> period_s(None)
    0.0

    A period has to be finite and no longer than a week. ``inf`` would
    make every budget infinite and every poll loop endless, which is the
    hang this module exists to have no part in.

    >>> period_s(None, "inf")
    0.0
    >>> period_s(float("inf"))
    0.0
    >>> period_s(86400.0 * 400.0)
    0.0
    """
    if derived_s is not None and _usable(float(derived_s)):
        return float(derived_s)
    if override:
        try:
            value = float(override)
        except ValueError:
            return 0.0
        if _usable(value):
            return value
    return 0.0


def _usable(value: float) -> bool:
    """A period is usable when it is positive and at most a week.

    ``value != value`` is the import-free test for a nan, which compares
    false against everything including itself. The upper bound is what
    keeps a nonsense epoch out: epoch 1 on a two-minute network implies a
    period of about nine years, and a budget that long is a hang.

    >>> _usable(120.0), _usable(0.0), _usable(-1.0)
    (True, False, False)
    >>> _usable(float("inf")), _usable(float("nan"))
    (False, False)
    >>> _usable(7.0 * 86400.0), _usable(7.0 * 86400.0 + 1.0)
    (True, False)
    """
    return value == value and 0.0 < value <= 7.0 * 86400.0


def budget_s(epoch_s: float, headroom_s: float) -> float:
    """One epoch plus ``headroom_s``.

    >>> budget_s(120.0, 180.0)
    300.0
    >>> budget_s(0.0, 180.0)
    180.0
    """
    return epoch_s + headroom_s
