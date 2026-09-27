import logging
import os
import uuid
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from katzenqt import katzen, network, persistent  # noqa: E402


class _Peer:
    def __init__(self, name: str) -> None:
        self.name = name


def test_ordinary_peer_is_displayable() -> None:
    assert katzen._peer_is_displayable(
        cast("persistent.ConversationPeer", _Peer("alice")),
    ) is True


def test_substream_peer_is_hidden() -> None:
    name = f"{network._SUBSTREAM_NAME_PREFIX}parent:00ff"
    assert katzen._peer_is_displayable(
        cast("persistent.ConversationPeer", _Peer(name)),
    ) is False


def test_a_peer_named_like_a_substream_suffix_stays_visible() -> None:
    assert katzen._peer_is_displayable(
        cast("persistent.ConversationPeer", _Peer("not:substream:x")),
    ) is True


def test_duration_time_ns_reads_the_raw_monotonic_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    class _RawClock:
        CLOCK_MONOTONIC_RAW = 4

        def clock_gettime_ns(self, clock_id: int) -> int:
            asked.append(clock_id)
            return 111

        def monotonic_ns(self) -> int:
            return 222

    monkeypatch.setattr(katzen, "time", _RawClock())
    assert katzen.duration_time_ns() == 111
    assert asked == [4]


def test_duration_time_ns_falls_back_to_monotonic_ns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PlainClock:
        def monotonic_ns(self) -> int:
            return 222

    monkeypatch.setattr(katzen, "time", _PlainClock())
    assert katzen.duration_time_ns() == 222


def test_get_all_loggers_reports_a_freshly_created_logger() -> None:
    name = f"katzenqt.test.{uuid.uuid4().hex}"
    logging.getLogger(name)
    assert name in katzen.get_all_loggers()


def test_first_instance_takes_the_lock(tmp_path: Path) -> None:
    state = tmp_path / "katzen.sqlite3"
    state.write_bytes(b"")
    original = katzen.persistent.state_file
    katzen.persistent.state_file = state
    held = None
    try:
        assert katzen.is_there_already_an_instance_running() is False
        held = katzen.already_running_fd
    finally:
        katzen.persistent.state_file = original
        if held is not None:
            held.close()


def test_second_instance_is_refused_while_the_lock_is_held(
    tmp_path: Path,
) -> None:
    state = tmp_path / "katzen.sqlite3"
    state.write_bytes(b"")
    original = katzen.persistent.state_file
    katzen.persistent.state_file = state
    held = None
    try:
        assert katzen.is_there_already_an_instance_running() is False
        held = katzen.already_running_fd
        assert katzen.is_there_already_an_instance_running() is True
    finally:
        katzen.persistent.state_file = original
        if held is not None:
            held.close()
        fd = getattr(katzen, "already_running_fd", None)
        if fd is not None and not fd.closed:
            fd.close()
