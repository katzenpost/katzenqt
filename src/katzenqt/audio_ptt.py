from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import sysconfig
from typing import Any

from . import persistent


class AudioEngineError(RuntimeError):
    pass


class AudioEngineUnavailable(AudioEngineError):
    pass


@dataclass(frozen=True, slots=True)
class VoiceNoteDraft:
    path: Path
    duration_seconds: float
    file_size_bytes: int


class PttAudioBridge:
    def __init__(
        self,
        cache_root: Path | None = None,
        backend_module: Any | None = None,
    ) -> None:
        self.cache_root = self._normalize_path(cache_root or (persistent.app_data / "audio"))
        # Keep editable drafts and cached received clips separate so review/discard
        # never races with playback of a received message.
        self.drafts_dir = self.cache_root / "drafts"
        self.received_dir = self.cache_root / "received"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.received_dir.mkdir(parents=True, exist_ok=True)

        backend_module = backend_module or _load_backend_module()
        self._engine = backend_module.PttAudioEngine(str(self.drafts_dir))
        self.active_draft_path: Path | None = None

    def start_capture(self, conversation_id: int) -> Path:
        draft_path = self._normalize_path(
            self._invoke("start_capture", f"conversation-{conversation_id}-draft")
        )
        self.active_draft_path = draft_path
        return draft_path

    def stop_capture(self) -> VoiceNoteDraft:
        clip = self._invoke("stop_capture")
        draft = VoiceNoteDraft(
            path=self._normalize_path(clip.path),
            duration_seconds=float(clip.duration_seconds),
            file_size_bytes=int(clip.file_size_bytes),
        )
        self.active_draft_path = draft.path
        return draft

    def cancel_capture(self) -> bool:
        cancelled = bool(self._invoke("cancel_capture"))
        if cancelled:
            self.active_draft_path = None
        return cancelled

    def play_preview(self, path: Path | str) -> None:
        self._invoke("play_preview", str(self._normalize_path(path)))

    def play_received(self, path: Path | str) -> None:
        self._invoke("play_received", str(self._normalize_path(path)))

    def stop_playback(self) -> None:
        self._invoke("stop_playback")

    def take_playback_error(self) -> str | None:
        method = getattr(self._engine, "take_playback_error", None)
        if method is None:
            return None
        try:
            error = method()
        except Exception as exc:  # pragma: no cover - exercised via PyO3 at runtime
            raise AudioEngineError(str(exc)) from exc
        if error is None:
            return None
        return str(error)

    @property
    def is_recording(self) -> bool:
        return bool(self._invoke("is_recording"))

    @property
    def is_playing(self) -> bool:
        return bool(self._invoke("is_playing"))

    def received_clip_path(self, message_id: str, basename: str) -> Path:
        safe_message_id = _safe_component(message_id)
        source_name = Path(basename)
        safe_stem = _safe_component(source_name.stem)
        suffix = source_name.suffix.lower() or ".opus"
        return self.received_dir / f"{safe_message_id}-{safe_stem}{suffix}"

    def cache_received_clip(self, message_id: str, basename: str, payload: bytes) -> Path:
        clip_path = self.received_clip_path(message_id, basename)
        # Received audio rows need a stable on-disk file for Rust playback, so
        # rewrite the cache entry only when the payload actually changed.
        if not clip_path.exists() or clip_path.read_bytes() != payload:
            clip_path.write_bytes(payload)
        return clip_path

    def is_draft_path(self, path: Path | str) -> bool:
        candidate = self._normalize_path(path)
        drafts_root = self._normalize_path(self.drafts_dir)
        return candidate == drafts_root or drafts_root in candidate.parents

    def discard_draft(self, path: Path | str) -> None:
        draft_path = self._normalize_path(path)
        if not self.is_draft_path(draft_path):
            return
        if self.active_draft_path == draft_path:
            self.active_draft_path = None
        draft_path.unlink(missing_ok=True)

    def _invoke(self, method_name: str, *args: object) -> Any:
        method = getattr(self._engine, method_name)
        try:
            return method(*args)
        except Exception as exc:  # pragma: no cover - exercised via PyO3 at runtime
            raise AudioEngineError(str(exc)) from exc

    @staticmethod
    def _normalize_path(path: Path | str) -> Path:
        return Path(path).expanduser().resolve(strict=False)


def _load_backend_module() -> Any:
    module_name = "rustic_audio_tool"
    # Prefer src/katzenqt/audio/rustic_audio_tool.so over any older site-packages
    # wheel. Cargo builds librustic_audio_tool.so; make rust-audio renames on install.
    direct_extension = _load_direct_backend_extension(module_name)
    if direct_extension is not None and _supports_playback_error_polling(direct_extension):
        return direct_extension

    try:
        backend_module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local build env
        if direct_extension is not None:
            return direct_extension

        raise AudioEngineUnavailable(
            "Rust audio extension is unavailable. Build rustic_audio_tool.so "
            f"separately and place it at {_package_audio_extension_path(module_name)}."
        ) from exc

    if _supports_playback_error_polling(backend_module):
        return backend_module

    if direct_extension is not None:
        return direct_extension

    return backend_module


def _supports_playback_error_polling(module: Any) -> bool:
    engine_type = getattr(module, "PttAudioEngine", None)
    return engine_type is not None and hasattr(engine_type, "take_playback_error")


def _load_direct_backend_extension(module_name: str) -> Any | None:
    extension_path = next(
        (candidate for candidate in _backend_extension_candidates(module_name) if candidate.exists()),
        None,
    )
    if extension_path is None:
        return None

    loader = importlib.machinery.ExtensionFileLoader(module_name, str(extension_path))
    spec = importlib.util.spec_from_file_location(module_name, extension_path, loader=loader)
    if spec is None:
        return None

    existing_module = sys.modules.pop(module_name, None)
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        loader.exec_module(module)
        return module
    except ImportError:
        return None
    finally:
        sys.modules.pop(module_name, None)
        if existing_module is not None:
            sys.modules[module_name] = existing_module


def _package_audio_extension_path(module_name: str) -> Path:
    """Path to the PyO3 shared library inside the package audio/ directory."""
    return Path(__file__).resolve().parent / "audio" / f"{module_name}.so"


def _backend_extension_candidates(module_name: str) -> tuple[Path, ...]:
    audio_dir = Path(__file__).resolve().parent / "audio"
    return (
        audio_dir / f"{module_name}.so",
        Path(sysconfig.get_path("platlib")) / f"{module_name}.so",
    )


def _safe_component(value: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "-"
        for char in value
    ).strip("-")
    return safe or "voice-note"