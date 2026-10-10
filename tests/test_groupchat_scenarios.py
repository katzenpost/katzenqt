"""Group chat scenarios against the simulated Pigeonhole, as golden files.
Set ``REWRITE=1`` to regenerate the expected output."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from katzenqt.groupchat import sim

SCENARIOS = sorted((Path(__file__).parent / "data" / "groupchat").glob("*.txt"))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda path: path.stem)
def test_scenario(path: Path) -> None:
    script = path.read_text()
    actual = sim.run(script)
    if os.environ.get("REWRITE"):
        path.write_text(actual)
    else:
        assert actual == script
