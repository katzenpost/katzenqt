from __future__ import annotations

import argparse
import json
import logging
import uuid
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from katzenqt import network, persistent
from katzenqt.headless import _actions
from katzenqt.headless import _args
from katzenqt.tally import controller as tally_controller
from katzenqt.tally import schema as tally_schema
from katzenqt.tally import sync as tally_sync

from tests.test_headless_actions_common import (
    ConvHandle,
    FakeClock,
    StubConnection,
    make_conversation,
    run_action,
)

ADDRESS = ["--address", "127.0.0.1:64331"]


@pytest.fixture(autouse=True)
def _logger_into_caplog(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    caplog.set_level(logging.INFO)


@pytest.fixture(autouse=True)
def _isolated_tally_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tally_controller.INSTANCE, "_docs", {})
    monkeypatch.setattr(tally_controller.INSTANCE, "_pending", {})


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> StubConnection:
    connection = StubConnection()
    monkeypatch.setattr(
        _actions,
        "_connect_and_start",
        AsyncMock(return_value=(connection, object())),
    )
    monkeypatch.setattr(_actions, "_shutdown", AsyncMock())
    return connection


async def _mark_every_plaintext_sent() -> None:
    async with persistent.asession() as sess:
        rows = (await sess.exec(select(persistent.PlaintextWAL))).all()
        for row in rows:
            sess.add(persistent.SentLog(id=row.id))
        await sess.commit()


async def _store_survey(
    handle: ConvHandle,
    *,
    survey_id: bytes,
    creator: "bytes | None" = None,
    slots: "list[str] | None" = None,
) -> None:
    doc = tally_schema.new_survey_doc(
        survey_id,
        "lunch?",
        tally_schema.Mode.APPROVAL,
        slots if slots is not None else ["mon", "tue"],
        creator=creator,
    )
    async with persistent.asession() as sess:
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=handle.conversation_id,
                doc_state=tally_sync.full_state(doc),
            )
        )
        await sess.commit()


@pytest.mark.asyncio
async def test_tally_list_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["tally-list", "ghost"]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_tally_list_reports_an_empty_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("bare")
    assert await run_action(["tally-list", "bare"]) == 0
    assert "(no surveys)" in caplog.text


@pytest.mark.asyncio
async def test_tally_list_prints_one_line_per_survey(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("polled")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    assert await run_action(["tally-list", "polled"]) == 0
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("SURVEY=")
    ]
    assert len(lines) == 1
    assert survey_id.hex() in lines[0]
    assert "status=open" in lines[0]
    assert "voters=0" in lines[0]
    assert "mode=approval" in lines[0]


@pytest.mark.asyncio
async def test_tally_create_rejects_an_unknown_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    args = argparse.Namespace(
        conv_name="any",
        topic="t",
        mode="sortition",
        slot=["a"],
        timeout=1.0,
    )
    assert (
        int(
            await _actions._action_tally_create(
                cast("_args.TallyCreate", args),
            )
        )
        == 2
    )
    assert "unknown mode 'sortition'" in caplog.text


@pytest.mark.asyncio
async def test_tally_create_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(
            ["tally-create", "ghost", "lunch?", "--slot", "mon", *ADDRESS]
        )
        == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_tally_create_broadcasts_and_reports_the_survey_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("planning")
    monkeypatch.setattr(_actions, "_send_one_gcm", AsyncMock(return_value=0))
    assert (
        await run_action(
            [
                "tally-create",
                "planning",
                "lunch?",
                "--mode",
                "availability",
                "--slot",
                "mon",
                "--slot",
                "tue",
                *ADDRESS,
            ]
        )
        == 0
    )
    created = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("TALLY_CREATED=")
    ]
    assert len(created) == 1
    async with persistent.asession() as sess:
        rows = (await sess.exec(select(persistent.TallyState))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_tally_create_stays_silent_when_the_broadcast_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("planning")
    monkeypatch.setattr(_actions, "_send_one_gcm", AsyncMock(return_value=3))
    assert (
        await run_action(
            ["tally-create", "planning", "lunch?", "--slot", "mon", *ADDRESS]
        )
        == 3
    )
    assert "TALLY_CREATED" not in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_rejects_a_malformed_slot_argument(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(
            [
                "tally-vote",
                "any",
                "--survey",
                uuid.uuid4().bytes.hex(),
                "--slot",
                "s0",
                *ADDRESS,
            ]
        )
        == 2
    )
    assert "must be SLOT_ID=availability" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_times_out_waiting_for_the_survey(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("waiting")
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "waiting",
                "--survey",
                uuid.uuid4().bytes.hex(),
                "--slot",
                "s0=yes",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "not received within 120s" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_refuses_an_unknown_conversation(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("host")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "ghost",
                "--survey",
                survey_id.hex(),
                "--slot",
                "s0=yes",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_rejects_a_vote_outside_the_mode_domain(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("voting")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "voting",
                "--survey",
                survey_id.hex(),
                "--slot",
                "s0=maybe",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 2
    )
    assert "invalid vote:" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_fails_when_the_survey_belongs_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = await make_conversation("owner")
    await make_conversation("bystander")
    survey_id = uuid.uuid4().bytes
    await _store_survey(owner, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "bystander",
                "--survey",
                survey_id.hex(),
                "--slot",
                "s0=yes",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "could not apply vote" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_broadcasts_and_reports_voted(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("voting")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    monkeypatch.setattr(network, "check_for_new", _mark_every_plaintext_sent)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "voting",
                "--survey",
                survey_id.hex(),
                "--slot",
                "s0=yes",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert "VOTED" in caplog.text


@pytest.mark.asyncio
async def test_tally_vote_times_out_waiting_for_its_own_send(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("voting")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    monkeypatch.setattr(network, "check_for_new", AsyncMock())
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-vote",
                "voting",
                "--survey",
                survey_id.hex(),
                "--slot",
                "s0=yes",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 3
    )
    assert "vote send timed out" in caplog.text


@pytest.mark.asyncio
async def test_tally_result_times_out_with_no_survey(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("counting")
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-result",
                "counting",
                "--survey",
                uuid.uuid4().bytes.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "tally result timed out" in caplog.text
    assert "TALLY=" not in caplog.text


@pytest.mark.asyncio
async def test_tally_result_emits_the_derived_tally(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("counting")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-result",
                "counting",
                "--survey",
                survey_id.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 0
    )
    line = next(
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("TALLY=")
    )
    assert json.loads(line.split("=", 1)[1])["survey_id"] == survey_id.hex()
    assert "WINNER=none (no yes votes)" in caplog.text


@pytest.mark.asyncio
async def test_tally_result_times_out_below_the_expected_voter_count(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("counting")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-result",
                "counting",
                "--survey",
                survey_id.hex(),
                "--expect-voters",
                "2",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "TALLY=" in caplog.text
    assert "tally result timed out" in caplog.text


@pytest.mark.asyncio
async def test_tally_close_times_out_waiting_for_the_survey(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("closing")
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-close",
                "closing",
                "--survey",
                uuid.uuid4().bytes.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "not received within 120s" in caplog.text


@pytest.mark.asyncio
async def test_tally_close_refuses_an_unknown_conversation(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("host")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-close",
                "ghost",
                "--survey",
                survey_id.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_tally_close_refuses_a_non_creator(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("closing")
    survey_id = uuid.uuid4().bytes
    await _store_survey(
        handle, survey_id=survey_id, creator=bytes([0x77]) * 16
    )
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-close",
                "closing",
                "--survey",
                survey_id.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "only the creator may" in caplog.text


@pytest.mark.asyncio
async def test_tally_close_broadcasts_and_reports_closed(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("closing")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    monkeypatch.setattr(network, "check_for_new", _mark_every_plaintext_sent)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-close",
                "closing",
                "--survey",
                survey_id.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert "CLOSED" in caplog.text


@pytest.mark.asyncio
async def test_tally_close_times_out_waiting_for_its_own_send(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("closing")
    survey_id = uuid.uuid4().bytes
    await _store_survey(handle, survey_id=survey_id)
    monkeypatch.setattr(network, "check_for_new", AsyncMock())
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "tally-close",
                "closing",
                "--survey",
                survey_id.hex(),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 3
    )
    assert "close send timed out" in caplog.text
