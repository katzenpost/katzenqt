from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration import _bounce_helpers as helpers

INTEGRATION = Path(__file__).resolve().parent / "integration"
LITERAL = re.compile(
    r"timeout=\s*(\d+(?:\.\d+)?)|_s = (\d+(?:\.\d+)?)|\"(\d{3,4})\"",
)


def _long_literals(text: str) -> list[str]:
    found = []
    for match in LITERAL.finditer(text):
        value = next(g for g in match.groups() if g is not None)
        if float(value) > 120:
            found.append(match.group(0))
    return found


def test_budget_for_adds_the_headroom_to_the_epoch() -> None:
    assert helpers.budget_for(120.0, 630.0) == 750.0
    assert helpers.budget_for(1200.0, 630.0) == 1830.0


def test_budget_s_reads_the_epoch_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KQT_EPOCH_DURATION_S", "300")
    helpers.epoch_duration_s.cache_clear()
    try:
        assert helpers.budget_s(630.0) == 930.0
        assert helpers.deadline_arg(780.0) == "1080"
    finally:
        helpers.epoch_duration_s.cache_clear()


def test_no_integration_wait_longer_than_a_docker_epoch_is_hardcoded() -> (
    None
):
    offenders = {
        path.name: hits
        for path in sorted(INTEGRATION.glob("test_*.py"))
        if (hits := _long_literals(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
