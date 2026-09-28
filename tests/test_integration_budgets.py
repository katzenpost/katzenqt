from __future__ import annotations

import ast
import asyncio
import logging
import re
import types
import uuid
from datetime import timedelta
from pathlib import Path

import cbor2
import pytest

from katzenqt import epochs, network, persistent
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


def test_the_epoch_budget_adds_the_headroom_to_the_period() -> None:
    assert epochs.budget_s(epochs.period_s(None), 180.0) == 180.0
    assert epochs.budget_s(epochs.period_s(None, "1200"), 180.0) == 1380.0
    assert epochs.budget_s(epochs.period_s(120.0), 180.0) == 300.0


def test_a_bad_epoch_override_is_ignored() -> None:
    assert epochs.period_s(None, "soon") == 0.0
    assert epochs.period_s(None, "0") == 0.0
    assert epochs.period_s(None, "") == 0.0
    assert epochs.period_s(None) == 0.0


def test_a_non_finite_period_is_refused_at_both_doors() -> None:
    for bad in ("inf", "1e999", "nan", "-inf"):
        assert epochs.period_s(None, bad) == 0.0, bad
    for derived in (float("inf"), float("nan"), float("-inf")):
        assert epochs.period_s(derived) == 0.0, derived
    assert epochs.period_s(float("inf"), "1200") == 1200.0


def test_a_period_longer_than_a_week_is_refused_at_both_doors() -> None:
    week = 7.0 * 86400.0
    assert epochs.period_s(week) == week
    assert epochs.period_s(week + 1.0) == 0.0
    assert epochs.period_s(None, str(week)) == week
    assert epochs.period_s(None, str(week + 1.0)) == 0.0
    assert epochs.period_s(week + 1.0, "1200") == 1200.0


def test_a_non_positive_derived_period_falls_back() -> None:
    assert epochs.period_s(0.0, "1200") == 1200.0
    assert epochs.period_s(-1.0, "1200") == 1200.0
    assert epochs.period_s(-1.0) == 0.0


def test_the_derived_period_beats_the_override() -> None:
    assert epochs.period_s(120.0, "1200") == 120.0


def test_the_network_reads_the_period_from_the_epoch_it_last_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KQT_EPOCH_DURATION_S", raising=False)
    monkeypatch.setattr(network, "_last_epoch", None)
    assert network.epoch_period_seconds() == 0.0
    monkeypatch.setenv("KQT_EPOCH_DURATION_S", "1200")
    assert network.epoch_period_seconds() == 1200.0
    monkeypatch.setattr(network, "_last_epoch", 2)
    now = network.KATZENPOST_EPOCH_ORIGIN + timedelta(seconds=240)
    assert network.epoch_period_seconds(now) == 120.0


def test_an_unusable_override_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(network, "_last_epoch", None)
    monkeypatch.setenv("KQT_EPOCH_DURATION_S", "soon")
    with caplog.at_level(logging.WARNING, logger="katzen.network"):
        assert network.epoch_period_seconds() == 0.0
    assert "ignoring KQT_EPOCH_DURATION_S" in caplog.text


def _epochs_tree() -> ast.Module:
    return ast.parse(
        Path(epochs.__file__).read_text(encoding="utf-8"),
        filename=epochs.__file__,
    )


def test_epochs_exposes_only_its_pure_functions() -> None:
    epochs.period_s(None)
    epochs.period_s(120.0, "1200")
    epochs.budget_s(120.0, 180.0)
    assert {
        name for name in vars(epochs) if not name.startswith("__")
    } == {"period_s", "budget_s", "_usable"}
    for name in ("period_s", "budget_s", "_usable"):
        fn = getattr(epochs, name)
        assert type(fn) is types.FunctionType, name
        assert fn.__closure__ is None, name
        assert fn.__dict__ == {}, name
        defaults = list(fn.__defaults__ or ()) + list(
            (fn.__kwdefaults__ or {}).values()
        )
        assert all(
            d is None or isinstance(d, (str, int, float, bool))
            for d in defaults
        ), name


def test_no_import_or_outer_rebinding_anywhere_in_epochs() -> None:
    banned = []
    for node in ast.walk(_epochs_tree()):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            banned.append(ast.dump(node))
        elif isinstance(node, ast.Import):
            banned += [a.name for a in node.names if a.name == "os"]
        elif isinstance(node, ast.ImportFrom):
            banned += [
                a.name for a in node.names
                if node.module == "os" or a.name in ("os", "environ")
            ]
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            banned.append(node.attr)
    assert banned == []


def test_the_same_arguments_always_give_the_same_answer() -> None:
    def readings() -> "list[float]":
        return [
            epochs.period_s(None),
            epochs.period_s(None, "1200"),
            epochs.period_s(120.0),
            epochs.budget_s(120.0, 180.0),
            epochs.budget_s(0.0, 180.0),
        ]

    before = readings()
    for _ in range(3):
        epochs.period_s(999.0, "4242")
        epochs.budget_s(999.0, 1.0)
        assert readings() == before


@pytest.mark.asyncio
async def test_a_fake_epoch_one_cannot_stall_wait_for_sent() -> None:
    await network.on_new_pki_document(
        {"payload": cbor2.dumps({"Epoch": 1})},
    )
    assert network.epoch_period_seconds() == 0.0
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await persistent.wait_for_sent(
        uuid.uuid4(), deadline_s=0.01,
        epoch_s=network.epoch_period_seconds(),
    ) is False
    assert loop.time() - started < 1.0


@pytest.mark.real_sleeps
@pytest.mark.asyncio
async def test_wait_for_sent_adds_the_epoch_to_the_deadline() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await persistent.wait_for_sent(
        uuid.uuid4(), deadline_s=0.05, epoch_s=0.2, poll_s=0.02,
    ) is False
    assert loop.time() - started >= 0.2


def test_the_hardcoded_wait_detector_fires() -> None:
    assert _long_literals("alice.wait(timeout=900)") == ["timeout=900"]
    assert _long_literals("alice.wait(timeout=120)") == []


def test_no_job_sets_a_wait_by_hand() -> None:
    text = NAMENLOS.read_text(encoding="utf-8")
    assert "KQT_SEND_BUDGET_FLOOR_S" not in text
    assert 'KQT_EPOCH_DURATION_S: "1200"' in text


def test_the_product_takes_no_hand_set_send_budget() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "katzenqt"
    hits = [
        path.name
        for path in root.rglob("*.py")
        if "KQT_SEND_BUDGET_FLOOR_S" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
