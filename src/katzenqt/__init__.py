"""katzenqt — Qt-based mixnet chat client.

The interactive GUI lives in ``katzenqt.katzen`` and drags PySide6 in at
import time. Headless consumers (the integration runner, tests that only
need ``persistent`` or ``models``, and anything else that just wants the
thin-client wiring) should not have to pay that cost or install the Qt
runtime libraries. To keep both working, ``cli`` is exposed lazily via
PEP 562: ``from katzenqt import cli`` and the
``katzenqt = "katzenqt:cli"`` console-script entry in pyproject.toml
both continue to resolve, but a plain ``import katzenqt`` (or
``from katzenqt import persistent`` etc.) no longer loads PySide6.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checker hint only
    from .katzen import cli as cli

__all__ = ["cli"]


def __getattr__(name: str):
    if name == "cli":
        from .katzen import cli
        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
