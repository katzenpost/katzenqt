from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import cbor2
import pytest

from katzenqt import models, persistent
from katzenqt.headless import _actions

from tests.test_headless_actions_common import (
    ConvHandle,
    FakeClock,
    StubConnection,
    add_log_row,
    make_conversation,
    run_action,
)

ADDRESS = ["--address", "127.0.0.1:64331"]
NO_MEMBERSHIP = bytes(32)


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


def _text_payload(text: str) -> bytes:
    gcm = models.GroupChatMessage(
        version=0,
        membership_hash=NO_MEMBERSHIP,
        text=text,
    )
    body: bytes = gcm.to_cbor()
    return b"F" + body


def _introduction_payload(display_name: str) -> bytes:
    gcm = models.GroupChatMessage(
        version=0,
        membership_hash=NO_MEMBERSHIP,
        introduction=models.GroupChatPleaseAdd(
            display_name=display_name,
            read_cap=bytes([0x44]) * 136,
        ),
    )
    body: bytes = gcm.to_cbor()
    return b"F" + body


def _marker_payload(fields: "dict[str, object]") -> bytes:
    return b"F" + cbor2.dumps(fields)


def _non_dict_payload() -> bytes:
    return b"F" + cbor2.dumps([1, 2, 3])


async def _peer_rows(handle: ConvHandle, payloads: "list[bytes]") -> None:
    peer_id = handle.peer_ids["bob"]
    for order, payload in enumerate(payloads):
        await add_log_row(
            conversation_id=handle.conversation_id,
            conversation_peer_id=peer_id,
            conversation_order=order,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_read_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run_action(["read", "ghost", "1", *ADDRESS]) == 2
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_read_times_out_with_no_peer_messages(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("quiet", peers=("bob",))
    clock = FakeClock()
    clock.install(monkeypatch)
    assert await run_action(["read", "quiet", "120", *ADDRESS]) == 1
    assert "TIMEOUT" in caplog.text
    assert clock.now == 120.0


@pytest.mark.asyncio
async def test_read_surfaces_the_first_peer_text(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("noisy", peers=("bob",))
    await _peer_rows(
        handle,
        [
            b"Cchunk",
            b"F\xff\xff\xff",
            _text_payload(""),
            _text_payload("hello there"),
        ],
    )
    await add_log_row(
        conversation_id=handle.conversation_id,
        conversation_peer_id=handle.own_peer_id,
        conversation_order=99,
        payload=_text_payload("mine, ignored"),
    )
    FakeClock().install(monkeypatch)
    assert await run_action(["read", "noisy", "120", *ADDRESS]) == 0
    assert "RECV=hello there" in caplog.text


@pytest.mark.asyncio
async def test_read_waits_for_the_expected_text(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("picky", peers=("bob",))
    await _peer_rows(handle, [_text_payload("not it")])
    FakeClock().install(monkeypatch)
    assert await run_action(["read", "picky", "120", "wanted", *ADDRESS]) == 1
    assert "RECV=" not in caplog.text
    assert "TIMEOUT" in caplog.text


@pytest.mark.asyncio
async def test_read_announces_an_introduction_once(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("growing", peers=("bob",))
    await _peer_rows(handle, [_introduction_payload("carol")])
    FakeClock().install(monkeypatch)
    assert await run_action(["read", "growing", "120", *ADDRESS]) == 1
    announcements = [
        r for r in caplog.records if r.getMessage().startswith("RECV_ADD=")
    ]
    assert len(announcements) == 1
    assert announcements[0].getMessage() == "RECV_ADD=bob added carol"


@pytest.mark.asyncio
async def test_read_file_refuses_an_unknown_conversation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(
            [
                "read-file",
                "ghost",
                "--to-dir",
                str(tmp_path / "out"),
                *ADDRESS,
            ]
        )
        == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_read_file_times_out_with_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("empty", peers=("bob",))
    clock = FakeClock()
    clock.install(monkeypatch)
    out = tmp_path / "out"
    assert (
        await run_action(
            [
                "read-file",
                "empty",
                "--to-dir",
                str(out),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 1
    )
    assert "TIMEOUT" in caplog.text
    assert out.is_dir()
    assert clock.now == 120.0


@pytest.mark.asyncio
async def test_read_file_skips_unusable_rows_and_copies_the_match(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(persistent, "state_file", tmp_path / "state.sqlite3")
    handle = await make_conversation("attached", peers=("bob",))
    body = b"the attachment body"
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "stored.bin").write_bytes(body)
    digest = hashlib.sha256(body).digest()
    await _peer_rows(
        handle,
        [
            b"Cchunk",
            b"F\xa1",
            _non_dict_payload(),
            _marker_payload({"kind": "other"}),
            _marker_payload(
                {
                    "kind": "file_marker",
                    "basename": "other.bin",
                    "rel_path": "blobs/stored.bin",
                    "sha256": digest,
                }
            ),
            _marker_payload({"kind": "file_marker", "basename": "want.bin"}),
            _marker_payload(
                {
                    "kind": "file_marker",
                    "basename": "want.bin",
                    "rel_path": "blobs/absent.bin",
                    "sha256": digest,
                }
            ),
            _marker_payload(
                {
                    "kind": "file_marker",
                    "basename": "want.bin",
                    "rel_path": "blobs/stored.bin",
                    "sha256": digest,
                }
            ),
        ],
    )
    out = tmp_path / "out"
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "read-file",
                "attached",
                "--to-dir",
                str(out),
                "--basename",
                "want.bin",
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert (out / "want.bin").read_bytes() == body
    assert f"RECV_FILE={(out / 'want.bin').resolve()}" in caplog.text
    assert "sha256 mismatch" not in caplog.text


@pytest.mark.asyncio
async def test_read_file_warns_on_a_digest_mismatch_but_still_copies(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(persistent, "state_file", tmp_path / "state.sqlite3")
    handle = await make_conversation("tampered", peers=("bob",))
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "stored.bin").write_bytes(b"real bytes")
    await _peer_rows(
        handle,
        [
            _marker_payload(
                {
                    "kind": "file_marker",
                    "basename": "claim.bin",
                    "rel_path": "blobs/stored.bin",
                    "sha256": hashlib.sha256(b"lie").digest(),
                }
            ),
        ],
    )
    out = tmp_path / "out"
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "read-file",
                "tampered",
                "--to-dir",
                str(out),
                "--timeout",
                "120",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert "sha256 mismatch for claim.bin" in caplog.text
    assert (out / "claim.bin").read_bytes() == b"real bytes"


@pytest.mark.asyncio
async def test_chat_session_refuses_an_unknown_conversation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        await run_action(["chat-session", "ghost", "SLEEP:1", *ADDRESS]) == 2
    )
    assert "conversation 'ghost' not found" in caplog.text


@pytest.mark.asyncio
async def test_chat_session_rejects_an_unknown_step(
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("stepping", peers=("bob",))
    assert (
        await run_action(["chat-session", "stepping", "DANCE:now", *ADDRESS])
        == 5
    )
    assert "STEP_FAIL:0:unknown-step:DANCE:now" in caplog.text


@pytest.mark.asyncio
async def test_chat_session_runs_send_read_and_sleep_to_completion(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handle = await make_conversation("session", peers=("bob",))
    await _peer_rows(handle, [b"Cchunk", b"F\xff\xff", _text_payload("pong")])
    monkeypatch.setattr(
        persistent, "wait_for_sent", AsyncMock(return_value=True)
    )
    FakeClock().install(monkeypatch)
    assert (
        await run_action(
            [
                "chat-session",
                "session",
                "SEND:ping",
                "READ:pong",
                "SLEEP:2",
                *ADDRESS,
            ]
        )
        == 0
    )
    assert "STEP_WAITING_ACK:0:SEND:ping" in caplog.text
    assert "STEP_OK:0:SEND:ping" in caplog.text
    assert "STEP_OK:1:READ:pong" in caplog.text
    assert "STEP_OK:2:SLEEP:2" in caplog.text
    assert "SESSION_DONE" in caplog.text


@pytest.mark.asyncio
async def test_chat_session_fails_a_send_that_is_never_acked(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("unacked", peers=("bob",))
    monkeypatch.setattr(
        persistent, "wait_for_sent", AsyncMock(return_value=False)
    )
    FakeClock().install(monkeypatch)
    assert (
        await run_action(["chat-session", "unacked", "SEND:ping", *ADDRESS])
        == 3
    )
    assert "STEP_FAIL:0:send-timeout:ping" in caplog.text


@pytest.mark.asyncio
async def test_chat_session_fails_a_read_that_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
    stub_session: StubConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_conversation("silent", peers=("bob",))
    clock = FakeClock()
    clock.install(monkeypatch)
    assert (
        await run_action(
            ["chat-session", "silent", "READ:pong:120", *ADDRESS]
        )
        == 4
    )
    assert "STEP_FAIL:0:read-timeout:pong" in caplog.text
    assert clock.now == 120.0
