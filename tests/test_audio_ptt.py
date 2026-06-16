from pathlib import Path
from unittest.mock import patch

import pytest

from katzenqt import audio_ptt
from katzenqt.audio_ptt import (
    _backend_extension_candidates,
    _load_backend_module,
    _package_audio_extension_path,
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


def test_start_and_stop_capture_use_managed_drafts_dir(tmp_path):
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)

    started_path = bridge.start_capture(42)
    draft = bridge.stop_capture()

    assert started_path == tmp_path.resolve() / "drafts" / "conversation-42-draft.opus"
    assert draft.path == started_path
    assert draft.duration_seconds == 1.5
    assert draft.file_size_bytes == 2048


def test_received_clip_path_sanitizes_message_id_and_filename(tmp_path):
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)

    clip_path = bridge.received_clip_path("message/42", "../voice note.opus")

    assert clip_path == tmp_path.resolve() / "received" / "message-42-voice-note.opus"


def test_discard_draft_only_removes_managed_voice_note(tmp_path):
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


def test_take_playback_error_clears_cached_backend_failure(tmp_path):
    bridge = PttAudioBridge(cache_root=tmp_path, backend_module=_FakeModule)
    bridge._engine.playback_error = "stream callback error: BufferUnderrun"

    assert bridge.take_playback_error() == "stream callback error: BufferUnderrun"
    assert bridge.take_playback_error() is None


def test_backend_extension_candidates_prefer_package_local_path():
    candidates = _backend_extension_candidates("rustic_audio_tool")

    assert candidates[0] == _package_audio_extension_path("rustic_audio_tool")
    assert candidates[0].name == "rustic_audio_tool.so"
    assert candidates[0].parent.name == "audio"


def test_load_backend_module_prefers_package_local_extension():
    installed_module = object()
    with patch.object(audio_ptt, "_load_direct_backend_extension", return_value=_FakeModule):
        with patch.object(audio_ptt.importlib, "import_module", return_value=installed_module):
            assert _load_backend_module() is _FakeModule


def test_load_backend_module_falls_back_to_direct_extension():
    with patch.object(audio_ptt, "_load_direct_backend_extension", return_value=_FakeModule):
        with patch.object(audio_ptt.importlib, "import_module", side_effect=ImportError("missing")):
            assert _load_backend_module() is _FakeModule


def test_load_backend_module_reports_missing_extension_path():
    with patch.object(audio_ptt, "_load_direct_backend_extension", return_value=None):
        with patch.object(audio_ptt.importlib, "import_module", side_effect=ImportError("missing")):
            with pytest.raises(AudioEngineUnavailable, match="audio/rustic_audio_tool.so"):
                _load_backend_module()