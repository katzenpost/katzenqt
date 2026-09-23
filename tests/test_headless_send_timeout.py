import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from katzenqt import persistent
from katzenqt.headless import _actions, _cli


@pytest.mark.parametrize("command", ["send", "send-file"])
def test_send_timeout_default_and_override(command):
    argv = [command, "demo", "payload", "--address", "127.0.0.1:64331"]
    assert _cli.parse(argv).args.timeout is None
    assert _cli.parse([*argv, "--timeout", "600"]).args.timeout == 600


@pytest.mark.parametrize("command", ["send", "send-file"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "1e999", "bad"])
def test_send_timeout_rejects_invalid_values(command, value):
    with pytest.raises(SystemExit) as exc:
        _cli.parse([
            command, "demo", "payload", "--address", "127.0.0.1:64331", f"--timeout={value}",
        ])
    assert exc.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "timeout", "expected_seconds", "expected_pending"),
    [
        ("send", None, 120, 1),
        ("send-file", None, 180, 3),
        ("send", 600, 600, 1),
        ("send-file", 600, 600, 3),
    ],
)
async def test_unacknowledged_send_expires_and_preserves_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture, command: str, timeout: float | None,
    expected_seconds: float, expected_pending: int,
) -> None:
    monkeypatch.setattr(_actions, "logger", logging.getLogger(__name__))
    stream = uuid.uuid4()
    async with persistent.asession() as sess:
        peer = persistent.ConversationPeer(name="self", read_cap_id=stream)
        sess.add_all([
            persistent.WriteCapWAL(id=stream),
            persistent.ReadCapWAL(id=stream, write_cap_id=stream),
            peer,
        ])
        await sess.commit()
        await sess.refresh(peer)
        sess.add(persistent.Conversation(
            name="demo", own_peer_id=peer.id, write_cap=stream,
        ))
        await sess.commit()

    clock = SimpleNamespace(now=0.0)

    async def advance(delay: float) -> None:
        clock.now += 60

    monkeypatch.setattr(_actions, "asyncio", SimpleNamespace(
        get_event_loop=lambda: SimpleNamespace(time=lambda: clock.now),
        sleep=advance,
    ))
    connection, background = object(), object()
    shutdown = AsyncMock()
    start = AsyncMock(return_value=(connection, background))
    monkeypatch.setattr(_actions, "_connect_and_start", start)
    monkeypatch.setattr(_actions, "_shutdown", shutdown)
    monkeypatch.setattr(_actions.network, "check_for_new", AsyncMock())

    payload = "hello"
    if command == "send-file":
        path = tmp_path / "attachment.bin"
        path.write_bytes(bytes((i * 17 + 11) & 255 for i in range(2000)))
        payload = str(path)
    argv = [command, "demo", payload, "--address", "127.0.0.1:64331"]
    if timeout is not None:
        argv.extend(["--timeout", str(timeout)])
    chosen = _cli.parse(argv)

    assert await chosen.func(chosen.args) == 3
    start.assert_awaited_once_with()
    assert clock.now == expected_seconds
    assert "send timed out waiting for SentLog" in caplog.text
    assert "SENT" not in caplog.text
    shutdown.assert_awaited_once_with(background, connection)
    async with persistent.asession() as sess:
        assert len((await sess.exec(select(persistent.PlaintextWAL))).all()) == expected_pending
        assert (await sess.exec(select(persistent.SentLog))).all() == []
