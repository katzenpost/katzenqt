from __future__ import annotations

import re
from pathlib import Path

import pytest

from katzenqt import persistent
from tests.integration import _bounce_helpers as helpers

INTEGRATION = Path(__file__).resolve().parent / "integration"
NAMENLOS = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "test-integration-namenlos.yml"
)
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


def test_the_voucher_bootstrap_waits_are_epoch_derived() -> None:
    text = (INTEGRATION / "_bounce_helpers.py").read_text(encoding="utf-8")
    body = text.split("def bootstrap_voucher", 1)[1].split("\ndef ", 1)[0]
    assert "budget_s(" in body
    assert _long_literals(body) == []


def test_the_ack_deadline_takes_the_send_budget_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KQT_SEND_BUDGET_FLOOR_S", raising=False)
    assert persistent.ack_deadline_s(180.0) == 180.0
    monkeypatch.setenv("KQT_SEND_BUDGET_FLOOR_S", "1500")
    assert persistent.ack_deadline_s(180.0) == 1500.0
    assert persistent.ack_deadline_s(1800.0) == 1800.0


def test_every_job_floors_its_acks_above_its_own_epoch() -> None:
    text = NAMENLOS.read_text(encoding="utf-8")
    floors = [
        float(v)
        for v in re.findall(r'KQT_SEND_BUDGET_FLOOR_S: "(\d+)"', text)
    ]
    match = re.search(r'KQT_EPOCH_DURATION_S: "(\d+)"', text)
    assert match is not None
    live = float(match.group(1))
    assert len(floors) == 4
    assert max(floors) > live
    assert min(floors) > 120.0
