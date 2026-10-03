from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

import pytest
from PySide6.QtCore import QUrl

T = TypeVar("T")


def ignore(*args: object, **kwargs: object) -> None:
    """Accept anything and return None.

    >>> ignore(1, two=2) is None
    True
    """
    return None


def returning(value: T) -> Callable[..., T]:
    """A callable that returns ``value`` whatever it is called with.

    >>> returning(3)("a", b=2)
    3
    """

    def _return(*args: object, **kwargs: object) -> T:
        return value

    return _return


def appending(target: list[T], value: T) -> Callable[..., None]:
    """A callable that appends ``value`` to ``target`` on every call.

    >>> calls: list[int] = []
    >>> record = appending(calls, 1)
    >>> record("x"); record()
    >>> calls
    [1, 1]
    """

    def _append(*args: object, **kwargs: object) -> None:
        target.append(value)

    return _append


def appending_from(
    target: list[T], fn: Callable[..., T]
) -> Callable[..., None]:
    """A callable that appends ``fn(*args, **kwargs)`` to ``target``.

    >>> seen: list[str] = []
    >>> record = appending_from(seen, str.upper)
    >>> record("a"); record("b")
    >>> seen
    ['A', 'B']
    """

    def _append(*args: object, **kwargs: object) -> None:
        target.append(fn(*args, **kwargs))

    return _append


def first_argument(first: T, *rest: object, **kwargs: object) -> T:
    """The first positional argument, as a recorder for ``appending_from``.

    >>> first_argument("name", object(), retry=True)
    'name'
    """
    return first


def call_now(ms: object, callback: Callable[[], object]) -> None:
    """Run ``callback`` at once; stands in for ``QTimer.singleShot``.

    >>> call_now(0, print)
    <BLANKLINE>
    """
    callback()


def local_file(url: QUrl) -> str:
    """The local path a ``QUrl`` names.

    >>> local_file(QUrl.fromLocalFile("/tmp/x.txt"))
    '/tmp/x.txt'
    """
    return url.toLocalFile()


def is_clear(event: asyncio.Event) -> bool:
    """True while ``event`` is not set.

    >>> is_clear(asyncio.Event())
    True
    """
    return not event.is_set()


def logged(caplog: pytest.LogCaptureFixture, text: str) -> bool:
    """True once a captured record's message contains ``text``.

    >>> from types import SimpleNamespace
    >>> records = [SimpleNamespace(message="drain: running")]
    >>> logged(SimpleNamespace(records=records), "running")
    True
    """
    return any(text in record.message for record in caplog.records)
