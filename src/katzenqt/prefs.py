from __future__ import annotations

import json
import logging

from . import persistent

logger = logging.getLogger("katzen.prefs")

PREFIX = "pref."

KNOWN_KEYS: "dict[str, type]" = {
    "theme.mode": str,
}


def _encode(value: object) -> "tuple[str, str]":
    """
    >>> _encode("dark")
    ('str', 'dark')
    >>> _encode(True), _encode(False)
    (('bool', '1'), ('bool', '0'))
    >>> _encode(20)
    ('int', '20')
    >>> _encode(1.5)
    ('float', '1.5')
    >>> _encode(["a"])
    ('json', '["a"]')
    """
    if isinstance(value, bool):
        return "bool", "1" if value else "0"
    if isinstance(value, int):
        return "int", str(value)
    if isinstance(value, float):
        return "float", repr(value)
    if isinstance(value, str):
        return "str", value
    return "json", json.dumps(value, sort_keys=True)


def _decode(kind: str, text: str) -> object:
    """
    >>> _decode("bool", "1"), _decode("bool", "0")
    (True, False)
    >>> _decode("int", "20"), _decode("float", "1.5")
    (20, 1.5)
    >>> _decode("json", '["a"]')
    ['a']
    >>> _decode("sometype", "x")
    Traceback (most recent call last):
        ...
    ValueError: unknown setting type: 'sometype'
    """
    if kind == "str":
        return text
    if kind == "bool":
        return text == "1"
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "json":
        return json.loads(text)
    raise ValueError(f"unknown setting type: {kind!r}")


def get(name: str, default: object, *, legacy: "str | None" = None) -> object:
    for key in (f"{PREFIX}{name}", legacy):
        if key is None:
            continue
        try:
            with persistent.Session(persistent._engine_sync) as sess:
                row = sess.get(persistent.AppSetting, key)
                if row is not None and row.value is not None:
                    return _decode(row.type, row.value)
        except Exception as e:
            logger.warning("could not read preference %s: %s", key, e)
            continue
    return default


def put(name: str, value: object) -> None:
    """Record a preference, if it is one katzenqt reads and the value
    fits the type recorded for it.

    Nothing else is written. A misspelled name, or a value of the wrong
    type for a name that is spelled right, would otherwise sit in the
    state file for good with nothing to read it back, and correcting it
    later would leave the bad row behind.
    """
    expected = KNOWN_KEYS.get(name)
    if expected is None:
        logger.warning(
            "not recording %s: no preference by that name is read", name,
        )
        return
    if not isinstance(value, expected):
        logger.warning(
            "not recording %s as %s: it is read as %s",
            name, type(value).__name__, expected.__name__,
        )
        return
    kind, text = _encode(value)
    key = f"{PREFIX}{name}"
    try:
        with persistent.Session(persistent._engine_sync) as sess:
            row = sess.get(persistent.AppSetting, key)
            if row is None:
                row = persistent.AppSetting(id=key)
            row.type = kind
            row.value = text
            sess.add(row)
            sess.commit()
    except Exception as e:
        logger.warning("could not persist preference %s: %s", key, e)
