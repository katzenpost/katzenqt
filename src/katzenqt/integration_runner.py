"""Legacy entry point for the headless CLI runner.

This module is preserved so the docker-integration tests under
``tests/integration/`` can keep invoking
``python -m katzenqt.integration_runner``. All behaviour now lives
in :mod:`katzenqt.headless`. New code should call
:func:`katzenqt.headless.cli` directly or invoke the
``katzenqt-headless`` console script.
"""
from __future__ import annotations

import sys

from . import headless


def main(argv: "list[str] | None" = None) -> int:
    return headless.cli(argv)


if __name__ == "__main__":
    sys.exit(main())
