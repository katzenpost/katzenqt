from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import click
import pytest

from katzenqt import network, persistent
from katzenqt.headless import _actions, _cli
from katzenqt.tally import engine as tally_engine
from katzenqt.tally import schema as tally_schema

from tests.fakes.thinclient import FakeThinClient
from tests.test_headless_actions_common import (
    FakeClock,
    add_log_row,
    StubConnection,
    make_conversation,
    run_action,
)


def _tally_with(votes: "list[dict[str, str]]") -> "tally_engine.TallyResult":
    doc = tally_schema.new_survey_doc(
        uuid.uuid4().bytes,
        "lunch?",
        tally_schema.Mode.APPROVAL,
        ["mon", "tue"],
    )
    for index, choice in enumerate(votes):
        tally_engine.apply_vote(doc, bytes([index + 1]) * 16, choice, 1)
    return tally_engine.tally(doc)


def test_seconds_accepts_a_finite_positive_number() -> None:
    assert _cli.seconds("12.5") == 12.5


@pytest.mark.parametrize("value", ["0", "-3", "nan", "inf", "junk"])
def test_seconds_rejects_bad_values(value: str) -> None:
    with pytest.raises(click.BadParameter):
        _cli.seconds(value)


def test_parse_slot_votes_builds_a_mapping() -> None:
    assert _actions._parse_slot_votes(["s0=yes", "s1=no"]) == {
        "s0": "yes",
        "s1": "no",
    }


def test_parse_slot_votes_rejects_a_vote_without_an_equals_sign() -> None:
    with pytest.raises(ValueError, match="must be SLOT_ID=availability"):
        _actions._parse_slot_votes(["s0"])


def test_tally_json_carries_slots_and_outcome() -> None:
    result = _tally_with([{"s0": "yes"}, {"s0": "yes", "s1": "no"}])
    payload = json.loads(_actions._tally_json(result))
    assert payload["mode"] == "approval"
    assert payload["status"] == "open"
    assert payload["n_voters"] == 2
    assert [s["slot_id"] for s in payload["slots"]] == ["s0", "s1"]
    assert payload["outcome"] == "winner"
    assert [w["slot_id"] for w in payload["winners"]] == ["s0"]


def test_declare_outcome_reports_a_single_winner() -> None:
    assert (
        _actions._declare_outcome(_tally_with([{"s0": "yes"}]))
        == "WINNER=mon (1 yes)"
    )


def test_declare_outcome_reports_a_tie() -> None:
    declared = _actions._declare_outcome(
        _tally_with([{"s0": "yes", "s1": "yes"}])
    )
    assert declared == "TIE=mon, tue (1 yes each)"


def test_declare_outcome_reports_no_winner() -> None:
    assert _actions._declare_outcome(_tally_with([{"s0": "no"}])) == (
        "WINNER=none (no yes votes)"
    )


def test_set_connection_config_records_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_actions, "_CONNECTION_CONFIG", None)
    _actions.set_connection_config("/tmp/thinclient.toml")
    assert _actions._CONNECTION_CONFIG == "/tmp/thinclient.toml"


def test_resolve_connection_config_passes_an_explicit_config_through() -> (
    None
):
    args = _cli.parse(["read", "--config", "/etc/tc.toml", "c", "1"]).args
    assert _actions.resolve_connection_config(args) == ("/etc/tc.toml", None)


@pytest.mark.parametrize(
    ("network_arg", "expected"),
    [("tcp", 'Network = "tcp"'), ("unix", "[Dial.Unix]")],
)
def test_resolve_connection_config_synthesises_a_toml_for_an_address(
    network_arg: str,
    expected: str,
) -> None:
    args = _cli.parse(
        [
            "read",
            "--address",
            "127.0.0.1:64331",
            "--network",
            network_arg,
            "c",
            "1",
        ]
    ).args
    path, temp_path = _actions.resolve_connection_config(args)
    try:
        assert path == temp_path
        body = Path(path).read_text()
        assert expected in body
        assert "127.0.0.1:64331" in body
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_conv_id_by_name_finds_and_misses() -> None:
    handle = await make_conversation("known")
    assert await _actions._conv_id_by_name("known") == handle.conversation_id
    assert await _actions._conv_id_by_name("unknown") is None


@pytest.mark.asyncio
async def test_conversation_by_name_returns_the_row_or_none() -> None:
    handle = await make_conversation("named")
    async with persistent.asession() as sess:
        found = await _actions._conversation_by_name(sess, "named")
        assert found is not None and found.id == handle.conversation_id
        assert await _actions._conversation_by_name(sess, "ghost") is None


@pytest.mark.asyncio
async def test_wait_for_conv_write_cap_sees_a_provisioned_cap() -> None:
    handle = await make_conversation("provisioned")
    assert (
        await _actions._wait_for_conv_write_cap(
            handle.conversation_id,
            attempts=2,
            delay=0.0,
        )
        is True
    )


@pytest.mark.asyncio
async def test_wait_for_conv_write_cap_gives_up_when_unprovisioned() -> None:
    handle = await make_conversation("bare", provision_write_cap=False)
    assert (
        await _actions._wait_for_conv_write_cap(
            handle.conversation_id,
            attempts=2,
            delay=0.0,
        )
        is False
    )


@pytest.mark.asyncio
async def test_wait_for_survey_finds_a_persisted_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = await make_conversation("surveyed")
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=handle.conversation_id,
                doc_state=b"blob",
            )
        )
        await sess.commit()
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await _actions._wait_for_survey(survey_id, 120.0) is True
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_wait_for_survey_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await _actions._wait_for_survey(uuid.uuid4().bytes, 120.0) is False
    assert clock.now == 120.0


@pytest.mark.asyncio
async def test_wait_for_sent_sees_the_sentlog_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwal_id = uuid.uuid4()
    async with persistent.asession() as sess:
        sess.add(persistent.SentLog(id=pwal_id))
        await sess.commit()
    FakeClock().install(monkeypatch)
    assert await _actions._wait_for_sent(pwal_id, 60.0) is True


@pytest.mark.asyncio
async def test_wait_for_sent_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await _actions._wait_for_sent(uuid.uuid4(), 120.0) is False
    assert clock.now == 120.0


@pytest.mark.asyncio
async def test_connect_and_start_dials_the_recorded_config(
    monkeypatch: pytest.MonkeyPatch,
    fake_thinclient: FakeThinClient,
) -> None:
    monkeypatch.setattr(
        _actions, "_CONNECTION_CONFIG", "/nowhere/thinclient.toml"
    )
    dialled: "list[str | None]" = []

    async def fake_reconnect(
        config_path: "str | None" = None,
    ) -> FakeThinClient:
        dialled.append(config_path)
        await network.on_connection_status(
            {"is_connected": True, "err": None}
        )
        return fake_thinclient

    monkeypatch.setattr(network, "reconnect", fake_reconnect)
    connection, bg = await _actions._connect_and_start()
    try:
        assert connection is fake_thinclient
        assert dialled == ["/nowhere/thinclient.toml"]
        assert not bg.done()
    finally:
        await _actions._shutdown(bg, connection, timeout=5.0)
    assert fake_thinclient.stopped is True


@pytest.mark.asyncio
async def test_connect_and_start_reconciles_the_tally_when_asked(
    monkeypatch: pytest.MonkeyPatch,
    fake_thinclient: FakeThinClient,
) -> None:
    reconciled: "list[int]" = []

    async def fake_reconcile(controller: object) -> None:
        reconciled.append(1)

    async def fake_reconnect(
        config_path: "str | None" = None,
    ) -> FakeThinClient:
        await network.on_connection_status(
            {"is_connected": True, "err": None}
        )
        return fake_thinclient

    monkeypatch.setattr(network, "reconnect", fake_reconnect)
    monkeypatch.setattr(
        type(_actions.tally_instance),
        "reconcile_from_log",
        fake_reconcile,
    )
    connection, bg = await _actions._connect_and_start(reconcile_tally=True)
    try:
        assert reconciled == [1]
    finally:
        await _actions._shutdown(bg, connection, timeout=5.0)


@pytest.mark.asyncio
async def test_shutdown_closes_the_connection_for_a_finished_task() -> None:
    async def done_quickly() -> None:
        return None

    bg = asyncio.ensure_future(done_quickly())
    connection = StubConnection()
    await _actions._shutdown(bg, connection, timeout=5.0)
    assert connection.stopped is True
    assert bg.done()


@pytest.mark.asyncio
async def test_shutdown_reraises_the_background_failure_after_closing() -> (
    None
):
    async def explode() -> None:
        raise RuntimeError("bg boom")

    bg = asyncio.ensure_future(explode())
    connection = StubConnection()
    with pytest.raises(RuntimeError, match="bg boom"):
        await _actions._shutdown(bg, connection, timeout=5.0)
    assert connection.stopped is True


@pytest.mark.asyncio
async def test_shutdown_warns_when_the_join_overruns_its_budget(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stuck = asyncio.Event()

    async def hang(tasks: object) -> None:
        await stuck.wait()

    monkeypatch.setattr(network, "_cancel_and_join", hang)
    bg = asyncio.ensure_future(asyncio.Event().wait())
    connection = StubConnection()
    with caplog.at_level(logging.WARNING):
        await _actions._shutdown(bg, connection, timeout=0.05)
    assert "did not finish cancelling" in caplog.text
    assert connection.stopped is True
    bg.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_action_info_reports_schema_conversations_and_wal_counts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    handle = await make_conversation(
        "reported",
        peers=("bob", f"{network._SUBSTREAM_NAME_PREFIX}x"),
    )
    await add_log_row(
        conversation_id=handle.conversation_id,
        conversation_peer_id=handle.own_peer_id,
        conversation_order=0,
        payload=b"Fhello",
    )
    with caplog.at_level(logging.INFO):
        assert await run_action(["info"]) == 0
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["state_file"] == str(persistent.state_file)
    assert payload["conversations"] == [
        {
            "id": handle.conversation_id,
            "name": "reported",
            "peer_count": 2,
            "messages": 1,
        }
    ]
    assert payload["wal"] == {"plaintext": 0, "mix": 0, "received_piece": 0}


@pytest.mark.asyncio
async def test_action_membership_hash_prints_a_digest(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    await make_conversation("hashed", peers=("bob",))
    with caplog.at_level(logging.INFO):
        assert await run_action(["membership-hash", "hashed"]) == 0
    line = caplog.records[-1].getMessage()
    assert line.startswith("MEMBERSHIP_HASH=")
    assert len(line.split("=", 1)[1]) == 64


@pytest.mark.asyncio
async def test_action_membership_hash_refuses_an_unknown_conversation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    with caplog.at_level(logging.ERROR):
        assert await run_action(["membership-hash", "ghost"]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_reraises_cancellation_after_the_join_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def joinable(tasks: object) -> None:
        await release.wait()

    async def done_quickly() -> None:
        return None

    monkeypatch.setattr(network, "_cancel_and_join", joinable)
    connection = StubConnection()
    shutting = asyncio.ensure_future(
        _actions._shutdown(
            asyncio.ensure_future(done_quickly()), connection, 30.0
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
    shutting.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutting
    assert connection.stopped is True
