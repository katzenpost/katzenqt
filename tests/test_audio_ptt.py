from pathlib import Path
from unittest.mock import patch

import pytest

from katzenqt import audio_ptt
from katzenqt.audio_ptt import (
    _load_backend_module,
    AudioEngineUnavailable,
    PttAudioBridge,
)


class _FakeClip:
    def __init__(self, path: str, duration_seconds: float, file_size_bytes: int):
        self.path = path
        self.duration_seconds = duration_seconds
        self.file_size_bytes = file_size_bytes


class _FakeEngine:
    def __init__(self, cache_dir: str):
        self.cache_dir = Path(cache_dir)
        self.current_path: Path | None = None
        self.preview_path: str | None = None
        self.received_path: str | None = None
        self.playback_error: str | None = None

    def start_capture(self, stem: str | None = None) -> str:
        self.current_path = self.cache_dir / f"{stem}.opus"
        return str(self.current_path)

    def stop_capture(self) -> _FakeClip:
        assert self.current_path is not None
        return _FakeClip(str(self.current_path), 1.5, 2048)

    def cancel_capture(self) -> bool:
        self.current_path = None
        return True

    def play_preview(self, path: str) -> None:
        self.preview_path = path

    def play_received(self, path: str) -> None:
        self.received_path = path

    def stop_playback(self) -> None:
        return None

    def take_playback_error(self) -> str | None:
        error = self.playback_error
        self.playback_error = None
        return error

    def is_recording(self) -> bool:
        return False

    def is_playing(self) -> bool:
        return False


class _FakeModule:
    PttAudioEngine = _FakeEngine


def test_start_and_stop_capture_use_managed_drafts_dir(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)

    started_path = bridge.start_capture(42)
    draft = bridge.stop_capture()

    assert started_path == tmp_path.resolve() / "drafts" / "conversation-42-draft.opus"
    assert draft.path == started_path
    assert draft.duration_seconds == 1.5
    assert draft.file_size_bytes == 2048


def test_received_clip_path_sanitizes_message_id_and_filename(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)

    clip_path = bridge.received_clip_path("message/42", "../voice note.opus")

    assert clip_path == tmp_path.resolve() / "received" / "message-42-voice-note.opus"


def test_discard_draft_only_removes_managed_voice_note(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    managed_draft = tmp_path / "drafts" / "voice-note.opus"
    external_file = tmp_path / "external.opus"
    managed_draft.parent.mkdir(parents=True, exist_ok=True)
    managed_draft.write_bytes(b"draft")
    external_file.write_bytes(b"external")

    bridge.discard_draft(managed_draft)
    bridge.discard_draft(external_file)

    assert not managed_draft.exists()
    assert external_file.exists()


def test_take_playback_error_clears_cached_backend_failure(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    bridge._engine.playback_error = "stream callback error: BufferUnderrun"

    assert bridge.take_playback_error() == "stream callback error: BufferUnderrun"
    assert bridge.take_playback_error() is None


def test_load_backend_module_returns_installed_extension() -> None:
    with patch.object(audio_ptt.importlib, "import_module", return_value=_FakeModule):
        assert _load_backend_module() is _FakeModule


def test_load_backend_module_reports_missing_extension() -> None:
    with patch.object(
        audio_ptt.importlib, "import_module", side_effect=ImportError("missing")
    ):
        with pytest.raises(AudioEngineUnavailable, match="not installed"):
            _load_backend_module()


def test_load_backend_module_rejects_extension_without_error_polling() -> None:
    class _OldModule:
        class PttAudioEngine:
            pass

    with patch.object(audio_ptt.importlib, "import_module", return_value=_OldModule):
        with pytest.raises(AudioEngineUnavailable, match="too old"):
            _load_backend_module()

def test_bridge_capture_playback_and_query_methods(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    bridge.start_capture(7)
    draft = bridge.stop_capture()
    assert draft.file_size_bytes == 2048
    assert bridge.active_draft_path == draft.path
    bridge.play_preview(draft.path)
    bridge.play_received(draft.path)
    bridge.stop_playback()
    assert bridge.is_recording is False
    assert bridge.is_playing is False
    bridge.start_capture(8)
    assert bridge.cancel_capture() is True
    assert bridge.active_draft_path is None


def test_cache_received_clip_writes_once_then_rewrites_on_change(
    tmp_path: Path,
) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    clip = bridge.cache_received_clip("m1", "note.opus", b"abc")
    assert clip.read_bytes() == b"abc"
    assert bridge.cache_received_clip("m1", "note.opus", b"abc") == clip
    bridge.cache_received_clip("m1", "note.opus", b"abcd")
    assert clip.read_bytes() == b"abcd"


def test_is_draft_path_and_discard_draft(tmp_path: Path) -> None:
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    draft = bridge.drafts_dir / "d.opus"
    draft.write_bytes(b"x")
    assert bridge.is_draft_path(draft) is True
    assert bridge.is_draft_path(bridge.received_dir / "r.opus") is False
    bridge.active_draft_path = bridge._normalize_path(draft)
    bridge.discard_draft(draft)
    assert not draft.exists()
    assert bridge.active_draft_path is None
    outside = tmp_path / "outside.opus"
    outside.write_bytes(b"y")
    bridge.discard_draft(outside)  # not a draft -> no-op
    assert outside.exists()


def test_take_playback_error_none_when_engine_lacks_method(
    tmp_path: Path,
) -> None:
    class _NoPollEngine:
        def __init__(self, cache_dir: str) -> None:
            pass

    class _NoPollModule:
        PttAudioEngine = _NoPollEngine

    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_NoPollModule)
    assert bridge.take_playback_error() is None
