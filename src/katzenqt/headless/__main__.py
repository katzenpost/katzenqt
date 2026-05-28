"""Allow ``python -m katzenqt.headless`` to invoke the CLI."""
import sys

from . import cli


if __name__ == "__main__":
    sys.exit(cli())
