from __future__ import annotations

import logging
import uuid
from base64 import b64encode
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from katzenqt import network, persistent
from katzenqt.headless import _actions

from tests.test_headless_actions_common import (
    OWN_READ_CAP,
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


@pytest.mark.asyncio
async def test_create_conv_reports_created_once_the_read_cap_lands(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def provisioning_connect(
        reconcile_tally: bool = False,
    ) -> "tuple[StubConnection, object]":
        async with persistent.asession() as sess:
            rows = (await sess.exec(select(persistent.ReadCapWAL))).all()
            for row in rows:
                row.read_cap = OWN_READ_CAP
                sess.add(row)
            await sess.commit()
        return StubConnection(), object()

    monkeypatch.setattr(_actions, "_connect_and_start", provisioning_connect)
    monkeypatch.setattr(_actions, "_shutdown", AsyncMock())

    assert await run_action(["create-conv", "fresh", "alice", *ADDRESS]) == 0
    assert "CREATED" in caplog.text
    async with persistent.asession() as sess:
        convo = (
            await sess.exec(
                select(persistent.Conversation).where(
                    persistent.Conversation.name == "fresh"
                )
            )
        ).first()
        assert convo is not None
        log_rows = (await sess.exec(select(persistent.ConversationLog))).all()
    assert len(log_rows) == 1


@pytest.mark.asyncio
async def test_create_conv_gives_up_when_no_read_cap_is_provisioned(
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(["create-conv", "stalled", "alice", *ADDRESS]) == 2
    )
    assert "no read_cap provisioned after timeout" in caplog.text
    assert "CREATED" not in caplog.text


@pytest.mark.asyncio
async def test_voucher_mint_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["voucher-mint", "ghost", "alice", *ADDRESS]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_voucher_mint_gives_up_without_a_provisioned_write_cap(
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("unprovisioned", provision_write_cap=False)
    assert (
        await run_action(["voucher-mint", "unprovisioned", "alice", *ADDRESS])
        == 2
    )
    assert "write cap not provisioned after timeout" in caplog.text


@pytest.mark.asyncio
async def test_voucher_mint_logs_the_base64_voucher(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    minted = AsyncMock(return_value=b"voucher-bytes")
    monkeypatch.setattr(_actions, "mint_and_publish", minted)
    await make_conversation("mintable")
    assert (
        await run_action(["voucher-mint", "mintable", "alice", *ADDRESS]) == 0
    )
    assert f"VOUCHER={b64encode(b'voucher-bytes').decode()}" in caplog.text
    assert minted.await_count == 1


@pytest.mark.asyncio
async def test_voucher_induct_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(["voucher-induct", "ghost", "bob", "AAAA", *ADDRESS])
        == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_voucher_induct_rejects_an_undecodable_voucher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("inducting")
    assert (
        await run_action(
            ["voucher-induct", "inducting", "bob", "abc", *ADDRESS]
        )
        == 2
    )
    assert "invalid voucher encoding" in caplog.text


@pytest.mark.asyncio
async def test_voucher_induct_logs_the_joiner(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    inducted = AsyncMock(return_value="bob")
    monkeypatch.setattr(_actions, "derive_read_and_induct", inducted)
    await make_conversation("inducting")
    payload = b64encode(b"voucher").decode()
    assert (
        await run_action(
            ["voucher-induct", "inducting", "bob", payload, *ADDRESS]
        )
        == 0
    )
    assert "INDUCTED=bob" in caplog.text
    assert inducted.await_args is not None
    assert inducted.await_args.args[3] == b"voucher"


@pytest.mark.asyncio
async def test_voucher_await_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["voucher-await", "ghost", *ADDRESS]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_voucher_await_logs_joined(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    opened = AsyncMock(return_value=None)
    monkeypatch.setattr(_actions, "await_and_open", opened)
    await make_conversation("joining")
    assert await run_action(["voucher-await", "joining", *ADDRESS]) == 0
    assert "JOINED" in caplog.text
    assert opened.await_count == 1


@pytest.mark.asyncio
async def test_send_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["send", "ghost", "hi", *ADDRESS]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_send_reports_sent_once_the_plaintext_clears(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("chatty", peers=("bob",))
    monkeypatch.setattr(network, "check_for_new", _mark_every_plaintext_sent)
    FakeClock().install(monkeypatch)
    assert await run_action(["send", "chatty", "hello", *ADDRESS]) == 0
    assert "SENT" in caplog.text
    async with persistent.asession() as sess:
        assert (await sess.exec(select(persistent.SentLog))).all() != []


@pytest.mark.asyncio
async def test_send_gives_up_after_its_whole_budget(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
) -> None:
    """One chunk waits the two-minute floor and then reports a timeout."""
    await make_conversation("floored")
    monkeypatch.delenv("KQT_SEND_BUDGET_FLOOR_S", raising=False)
    monkeypatch.setattr(network, "check_for_new", AsyncMock())
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await run_action(["send", "floored", "hello", *ADDRESS]) == 3
    assert clock.now == 120.0


@pytest.mark.asyncio
async def test_send_file_refuses_a_missing_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "nope.bin"
    assert await run_action(["send-file", "any", str(missing), *ADDRESS]) == 2
    assert "file not found" in caplog.text


@pytest.mark.asyncio
async def test_send_file_refuses_a_file_over_the_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(network, "_ATTACHMENT_HARD_CAP", 4)
    path = tmp_path / "big.bin"
    path.write_bytes(b"12345678")
    assert await run_action(["send-file", "any", str(path), *ADDRESS]) == 2
    assert "exceeds 4-byte cap" in caplog.text


@pytest.mark.asyncio
async def test_send_file_reports_sent_with_overridden_metadata(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("filedrop")
    path = tmp_path / "note.txt"
    path.write_bytes(bytes((i * 7 + 3) & 255 for i in range(4000)))
    monkeypatch.setattr(network, "check_for_new", _mark_every_plaintext_sent)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "send-file",
                "filedrop",
                str(path),
                "--basename",
                "renamed.txt",
                "--filetype",
                "text/plain",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert "SENT" in caplog.text
    async with persistent.asession() as sess:
        caps = (await sess.exec(select(persistent.WriteCapWAL))).all()
    assert len(caps) == 2
    assert handle.write_cap_id in {cap.id for cap in caps}


@pytest.mark.asyncio
async def test_multi_send_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["multi-send", "ghost", "a|b", *ADDRESS]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_multi_send_queues_every_text_then_waits_for_the_last(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("burst", peers=("bob",))
    monkeypatch.setattr(network, "check_for_new", _mark_every_plaintext_sent)
    FakeClock().install(monkeypatch)
    assert (
        await run_action(["multi-send", "burst", "one|two|three", *ADDRESS])
        == 0
    )
    assert "SENT" in caplog.text
    async with persistent.asession() as sess:
        sent = (await sess.exec(select(persistent.SentLog))).all()
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_multi_send_times_out_when_nothing_clears(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("stuck")
    monkeypatch.setattr(network, "check_for_new", AsyncMock())
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await run_action(["multi-send", "stuck", "only", *ADDRESS]) == 3
    assert "multi-send timed out" in caplog.text
    assert clock.now == 600.0


@pytest.mark.asyncio
async def test_send_stages_rows_against_the_conversation_write_cap(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
) -> None:
    handle = await make_conversation("streamed")
    monkeypatch.setattr(network, "check_for_new", AsyncMock())
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            ["send", "streamed", "hi", "--timeout", "1", *ADDRESS]
        )
        == 3
    )
    async with persistent.asession() as sess:
        rows = (await sess.exec(select(persistent.PlaintextWAL))).all()
    assert [row.bacap_stream for row in rows] == [handle.write_cap_id]
    assert isinstance(rows[0].id, uuid.UUID)
