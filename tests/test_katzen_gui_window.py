from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from typing import cast

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from katzenqt import katzen, models, persistent  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    PNG_BYTES,
    FakeAudio,
    FakeMessageBox,
    audio,
    boxes,
    instant_timer,
    loaded_window,
    qt_app,
    window,
)
from functools import partial
from tests.stubs import appending_from, local_file, returning


async def settle(rounds: int = 60) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def queue_attachment(win: katzen.MainWindow, path: Path) -> None:
    convo = win.convo_state()
    convo.attached_files.add(str(path))
    win.refresh_attached_files_for_conversation(convo)


def insert_log_row(payload: bytes, network_status: int = 0) -> str:
    message_uuid = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.ConversationLog(
                id=message_uuid,
                conversation_id=1,
                conversation_peer_id=1,
                conversation_order=0,
                payload=payload,
                network_status=network_status,
            )
        )
        sess.commit()
    return str(message_uuid)


def test_the_window_builds_its_attachment_controls(
    window: katzen.MainWindow,
) -> None:
    assert window.windowTitle() == katzen.APP_NAME
    assert window.preview_attachment_button.text() == "Preview clip"
    assert window.stop_attachment_audio_button.text() == "Stop audio"
    assert window.discard_attachment_button.text() == "Remove attachment"
    tab = window.ui.attach_file_tab
    assert window.preview_attachment_button.isEnabledTo(tab) is False
    assert window.stop_attachment_audio_button.isEnabledTo(tab) is False
    assert window.discard_attachment_button.isEnabledTo(tab) is False
    assert window.ui.attached_files_QListWidget.isEnabledTo(tab) is True
    assert window.ui.action_theme.isEnabled() is True
    assert window.ui.menuMixnetStatus.isEnabled() is True
    assert window.push_to_talk_started is False
    assert playing_id(window) == ""
    assert window._poll_windows == {}


def test_the_mixnet_status_menu_gains_three_actions(
    window: katzen.MainWindow,
) -> None:
    labels = [a.text() for a in window.ui.menuMixnetStatus.actions()]
    assert labels[-3:] == ["Stats", "Network consensus", "Packets"]


def test_the_composer_is_clamped_to_the_current_tab(
    window: katzen.MainWindow,
) -> None:
    tabs = window.ui.singlemultitab
    window._fit_composer()
    chrome = tabs.tabBar().sizeHint().height() + 2 * tabs.style().pixelMetric(
        katzen.QStyle.PixelMetric.PM_DefaultFrameWidth,
    )
    expected = tabs.currentWidget().sizeHint().height() + chrome
    assert tabs.maximumHeight() == expected


def test_no_selection_means_no_attachment_path(
    window: katzen.MainWindow,
) -> None:
    assert window._selected_attachment_item() is None
    assert window._selected_attachment_path() is None
    assert window._is_previewable_attachment(None) is False


def test_an_item_without_a_stored_path_resolves_to_none(
    window: katzen.MainWindow,
) -> None:
    item = katzen.QListWidgetItem("orphan")
    item.setData(0x100, "")
    window.ui.attached_files_QListWidget.addItem(item)
    window.ui.attached_files_QListWidget.setCurrentItem(item)
    assert window._selected_attachment_item() is item
    assert window._selected_attachment_path() is None


def playing_id(win: katzen.MainWindow) -> str:
    """The QML-visible playing-message property, as QML reads it."""
    return cast(str, win.playingMessageId)


@pytest.mark.asyncio
async def test_the_attachment_row_shows_a_byte_sized_label(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    queue_attachment(loaded_window, empty)
    item = loaded_window.ui.attached_files_QListWidget.item(0)
    assert item.text() == f"{tmp_path.name}/empty.bin\r\n(0B)"
    assert item.toolTip() == str(empty)
    assert loaded_window.ui.send_file_button.isEnabled() is True


@pytest.mark.asyncio
async def test_the_attachment_row_scales_to_kibibytes(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    chunk = tmp_path / "chunk.bin"
    chunk.write_bytes(b"\0" * 1536)
    queue_attachment(loaded_window, chunk)
    item = loaded_window.ui.attached_files_QListWidget.item(0)
    assert item.text().endswith("\r\n(1.5K)")


@pytest.mark.asyncio
async def test_the_attachment_row_scales_to_mebibytes(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(3 * 1024 * 1024)
    queue_attachment(loaded_window, big)
    item = loaded_window.ui.attached_files_QListWidget.item(0)
    assert item.text().endswith("\r\n(3M)")


@pytest.mark.asyncio
async def test_a_vanished_attachment_is_skipped(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    convo = loaded_window.convo_state()
    convo.attached_files.add(str(tmp_path / "never-existed.bin"))
    loaded_window.refresh_attached_files_for_conversation(convo)
    assert loaded_window.ui.attached_files_QListWidget.count() == 0
    assert loaded_window.ui.send_file_button.isEnabled() is False
    assert loaded_window.discard_attachment_button.isEnabled() is False


@pytest.mark.asyncio
async def test_an_opus_attachment_is_labelled_as_an_audio_clip(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    clip = tmp_path / "note.opus"
    clip.write_bytes(b"opus")
    queue_attachment(loaded_window, clip)
    item = loaded_window.ui.attached_files_QListWidget.item(0)
    assert item.text().startswith("audio clip\r\n")
    assert loaded_window.preview_attachment_button.isEnabled() is True
    assert loaded_window.discard_attachment_button.text() == (
        "Remove attachment"
    )


@pytest.mark.asyncio
async def test_a_voice_note_draft_is_labelled_and_offers_discard(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    draft = audio.start_capture(7)
    queue_attachment(loaded_window, draft)
    item = loaded_window.ui.attached_files_QListWidget.item(0)
    assert item.text().startswith("voice note draft\r\n")
    assert (
        loaded_window.discard_attachment_button.text() == "Discard voice note"
    )
    assert loaded_window.stop_attachment_audio_button.isEnabled() is True


@pytest.mark.asyncio
async def test_refresh_keeps_the_selected_row_selected(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    queue_attachment(loaded_window, first)
    queue_attachment(loaded_window, second)
    listing = loaded_window.ui.attached_files_QListWidget
    target = next(
        listing.item(row)
        for row in range(listing.count())
        if listing.item(row).toolTip() == str(second)
    )
    listing.setCurrentItem(target)
    loaded_window.refresh_attached_files_for_conversation(
        loaded_window.convo_state(),
    )
    assert loaded_window._selected_attachment_path() == second


@pytest.mark.asyncio
async def test_discarding_a_plain_attachment_drops_it_from_the_convo(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"hello")
    queue_attachment(loaded_window, doc)
    loaded_window._discard_selected_attachment()
    assert loaded_window.convo_state().attached_files == set()
    assert loaded_window.ui.attached_files_QListWidget.count() == 0
    assert doc.is_file() is True


@pytest.mark.asyncio
async def test_discarding_a_voice_note_deletes_the_draft(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    draft = audio.start_capture(3)
    queue_attachment(loaded_window, draft)
    loaded_window._discard_selected_attachment()
    assert audio.discarded == [draft]
    assert audio.stops == 1
    assert draft.exists() is False
    assert loaded_window.convo_state().attached_files == set()


@pytest.mark.asyncio
async def test_discarding_survives_a_failing_stop_playback(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"x")
    queue_attachment(loaded_window, doc)
    audio.raise_on.add("stop_playback")
    loaded_window._discard_selected_attachment()
    assert loaded_window.convo_state().attached_files == set()


def test_discarding_without_a_selection_does_nothing(
    window: katzen.MainWindow,
) -> None:
    window._discard_selected_attachment()
    assert window.ui.attached_files_QListWidget.count() == 0


def test_the_status_bar_joins_the_title_and_message(
    window: katzen.MainWindow,
) -> None:
    window._show_status_message("Voice note attached", "note.opus (1.5s)")
    assert window.statusBar().currentMessage() == (
        "Voice note attached: note.opus (1.5s)"
    )
    window._show_status_message("", "bare")
    assert window.statusBar().currentMessage() == "bare"


def test_the_playing_message_id_property_notifies_on_change(
    window: katzen.MainWindow,
) -> None:
    seen: list[str] = []
    window.playingMessageIdChanged.connect(
        appending_from(seen, partial(getattr, window, "playingMessageId")),
    )
    window._set_playing_message_id("abc")
    window._set_playing_message_id("abc")
    window._set_playing_message_id("")
    assert seen == ["abc", ""]
    assert playing_id(window) == ""


def test_an_unavailable_audio_engine_is_reported_once(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise katzen.AudioEngineUnavailable("no sound card")

    monkeypatch.setattr(katzen, "PttAudioBridge", boom)
    assert window._push_to_talk_audio() is None
    assert window._ptt_audio_failed is True
    assert boxes.texts() == ["no sound card"]
    assert window._push_to_talk_audio() is None
    assert len(boxes.seen) == 1


def test_a_working_audio_engine_is_cached(
    window: katzen.MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[FakeAudio] = []

    def build() -> FakeAudio:
        bridge = FakeAudio(tmp_path / "engine")
        made.append(bridge)
        return bridge

    monkeypatch.setattr(katzen, "PttAudioBridge", build)
    first = window._push_to_talk_audio()
    second = window._push_to_talk_audio()
    assert first is second
    assert len(made) == 1
    assert (
        window.stop_attachment_audio_button.isEnabledTo(
            window.ui.attach_file_tab,
        )
        is True
    )


def test_the_playback_monitor_stops_when_no_engine_exists(
    window: katzen.MainWindow,
) -> None:
    window._start_playback_monitor("boom")
    assert window._playback_error_timer.isActive() is True
    window._poll_playback_error()
    assert window._playback_error_timer.isActive() is False
    assert window._playback_failure_message is None


def test_a_playback_error_is_reported_and_stops_the_monitor(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    window._start_playback_monitor("Failed to play the received voice note.")
    window._set_playing_message_id("m1")
    audio.playback_error = "device lost"
    window._poll_playback_error()
    assert boxes.seen[0].kind == "critical"
    assert boxes.seen[0].text == (
        "Failed to play the received voice note.\n\ndevice lost"
    )
    assert playing_id(window) == ""
    assert window._playback_error_timer.isActive() is False


def test_a_late_playback_error_is_still_reported(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    calls: list[int] = []

    def take() -> str | None:
        calls.append(len(calls))
        return "late failure" if len(calls) == 2 else None

    audio.take_playback_error = take  # type: ignore[method-assign]
    audio.is_playing = False
    window._start_playback_monitor("")
    window._poll_playback_error()
    assert boxes.seen[0].text == "Audio playback failed.\n\nlate failure"


def test_finished_playback_quietly_stops_the_monitor(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    audio.is_playing = False
    window._start_playback_monitor("unused")
    window._set_playing_message_id("m9")
    window._poll_playback_error()
    assert boxes.seen == []
    assert playing_id(window) == ""
    assert window._playback_error_timer.isActive() is False


def test_a_broken_monitor_call_is_reported(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    audio.raise_on.add("take_playback_error")
    window._start_playback_monitor("unused")
    window._poll_playback_error()
    assert boxes.seen[0].text == (
        "Failed to monitor audio playback.\n\ntake_playback_error failed"
    )


@pytest.mark.asyncio
async def test_previewing_plays_the_selected_clip(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    tmp_path: Path,
) -> None:
    clip = tmp_path / "note.opus"
    clip.write_bytes(b"opus")
    queue_attachment(loaded_window, clip)
    loaded_window._play_attachment_preview()
    assert audio.previewed == [clip]
    assert loaded_window._playback_failure_message == (
        "Failed to preview the selected audio clip."
    )


@pytest.mark.asyncio
async def test_previewing_a_non_audio_attachment_does_nothing(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"x")
    queue_attachment(loaded_window, doc)
    loaded_window._play_attachment_preview()
    assert audio.previewed == []


@pytest.mark.asyncio
async def test_a_failed_preview_is_reported(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
    tmp_path: Path,
) -> None:
    clip = tmp_path / "note.opus"
    clip.write_bytes(b"opus")
    queue_attachment(loaded_window, clip)
    audio.raise_on.add("play_preview")
    loaded_window._play_attachment_preview()
    assert boxes.seen[0].text == (
        "Failed to preview the selected audio clip.\n\nplay_preview failed"
    )


def test_stopping_playback_without_an_engine_is_a_no_op(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    window.stopAudioPlayback()
    assert boxes.seen == []


def test_stopping_playback_clears_the_monitor(
    window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    window._start_playback_monitor("x")
    window._set_playing_message_id("m2")
    window.stopAudioPlayback()
    assert audio.stops == 1
    assert playing_id(window) == ""
    assert window._playback_error_timer.isActive() is False


def test_a_failing_stop_is_reported(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    audio.raise_on.add("stop_playback")
    window.stopAudioPlayback()
    assert boxes.seen[0].text == (
        "Failed to stop audio playback.\n\nstop_playback failed"
    )


def test_resolving_a_non_uuid_message_id_returns_none(
    window: katzen.MainWindow,
) -> None:
    assert window._resolve_attachment("not-a-uuid") is None


def test_resolving_an_unknown_message_id_returns_none(
    window: katzen.MainWindow,
) -> None:
    assert window._resolve_attachment(str(uuid.uuid4())) is None


def test_a_plain_text_row_is_not_an_attachment(
    window: katzen.MainWindow,
) -> None:
    assert window._resolve_attachment(insert_log_row(b"just text")) is None


def test_an_undecodable_body_is_not_an_attachment(
    window: katzen.MainWindow,
) -> None:
    assert (
        window._resolve_attachment(insert_log_row(b"F\xff\xff\xff")) is None
    )


def test_a_marker_of_an_unknown_kind_is_ignored(
    window: katzen.MainWindow,
) -> None:
    payload = b"F" + cbor2.dumps({"kind": "file_something_new"})
    assert window._resolve_attachment(insert_log_row(payload)) is None


def test_a_file_marker_without_a_location_is_refused(
    window: katzen.MainWindow,
) -> None:
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_marker",
            "basename": "doc.pdf",
            "rel_path": "",
        }
    )
    with pytest.raises(katzen._AttachmentError) as caught:
        window._resolve_attachment(insert_log_row(payload))
    assert str(caught.value) == "doc.pdf has no stored location."


def test_a_message_without_a_file_upload_resolves_to_none(
    window: katzen.MainWindow,
) -> None:
    gcm = models.GroupChatMessage(
        version=0, text="hi"
    )
    assert window._resolve_attachment(
        insert_log_row(b"F" + gcm.to_cbor())
    ) is (None)


def test_a_legacy_inline_upload_is_spilled_to_a_cache_file(
    window: katzen.MainWindow,
) -> None:
    upload = models.GroupChatFileUpload(
        basename="report.txt",
        filetype="text/plain",
        payload=b"inline bytes",
    )
    gcm = models.GroupChatMessage(
        version=0,
        file_upload=upload,
    )
    message_id = insert_log_row(b"F" + gcm.to_cbor())
    resolved = window._resolve_attachment(message_id)
    assert resolved is not None
    assert resolved.basename == "report.txt"
    assert resolved.filetype == "text/plain"
    assert resolved.path.read_bytes() == b"inline bytes"
    assert resolved.path.parent.name == "_inline_cache"
    assert resolved.received is True
    stamp = resolved.path.stat().st_mtime_ns
    again = window._resolve_attachment(message_id)
    assert again is not None
    assert again.path.stat().st_mtime_ns == stamp


def spill_received_marker(basename: str, blob: bytes) -> str:
    rel_path = f"attachments/42/{uuid.uuid4().hex}-{basename}"
    target = persistent.state_file.parent / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    payload = b"F" + cbor2.dumps(
        {
            "v": 0,
            "kind": "file_marker",
            "basename": basename,
            "filetype": "application/octet-stream",
            "size": len(blob),
            "rel_path": rel_path,
            "sha256": hashlib.sha256(blob).digest(),
        }
    )
    return insert_log_row(payload)


def test_playing_a_received_voice_note_caches_and_plays_it(
    window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    message_id = spill_received_marker("note.opus", b"opus bytes")
    window.playReceivedMessage(message_id)
    assert len(audio.played) == 1
    assert audio.played[0].read_bytes() == b"opus bytes"
    assert playing_id(window) == message_id
    assert window._playback_error_timer.isActive() is True


def test_playing_without_an_audio_engine_does_nothing(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    window._ptt_audio_failed = True
    message_id = spill_received_marker("n.opus", b"opus bytes")
    window.playReceivedMessage(message_id)
    assert audio.played == []
    assert boxes.seen == []
    assert playing_id(window) == ""

    window._ptt_audio_failed = False
    window.playReceivedMessage(message_id)
    assert len(audio.played) == 1
    assert playing_id(window) == message_id


def test_playing_a_missing_attachment_warns(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_oversized",
            "basename": "huge.bin",
        }
    )
    window.playReceivedMessage(insert_log_row(payload))
    assert boxes.seen[0].text == (
        "huge.bin was too large to receive and its contents were dropped."
    )
    assert boxes.seen[0].kind == "exec"
    assert audio.played == []


def test_playing_an_unresolvable_note_says_so(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    window.playReceivedMessage("still-not-a-uuid")
    assert boxes.seen == [
        (
            "information",
            katzen.APP_NAME,
            "This voice note is not available locally yet.",
        ),
    ]


def test_a_failing_playback_start_is_reported(
    window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
) -> None:
    audio.raise_on.add("play_received")
    window.playReceivedMessage(spill_received_marker("note.opus", b"opus"))
    assert playing_id(window) == ""
    assert boxes.seen[0].text == (
        "Failed to play the received voice note.\n\nplay_received failed"
    )


def test_opening_an_attachment_asks_first_and_then_opens(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        katzen.QDesktopServices,
        "openUrl",
        staticmethod(appending_from(opened, local_file)),
    )
    boxes.answer = QMessageBox.StandardButton.Yes
    message_id = spill_received_marker("notes.txt", b"plain")
    window.openAttachment(message_id)
    assert len(opened) == 1
    assert opened[0].endswith("notes.txt")
    assert "Name: notes.txt" in boxes.seen[0].text
    assert "Type: application/octet-stream" in boxes.seen[0].text
    assert "Warning: files of this kind" not in boxes.seen[0].text


def test_declining_the_prompt_leaves_the_attachment_unopened(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        katzen.QDesktopServices,
        "openUrl",
        staticmethod(appending_from(opened, local_file)),
    )
    window.openAttachment(spill_received_marker("notes.txt", b"plain"))
    assert opened == []


def test_a_risky_extension_adds_the_warning_paragraph(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    resolved = katzen._ResolvedAttachment(
        "payload.svg", "image/svg+xml", Path("/tmp/payload.svg")
    )
    assert window._confirm_open_attachment(resolved) is False
    assert "Warning: files of this kind can open in a browser" in (
        boxes.seen[0].text
    )
    assert boxes.seen[0].text.startswith("Open this attachment received")


def test_an_unknown_filetype_is_named_in_the_prompt(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    resolved = katzen._ResolvedAttachment("thing", None, Path("/tmp/thing"))
    boxes.answer = QMessageBox.StandardButton.Yes
    assert window._confirm_open_attachment(resolved) is True
    assert "Type: unknown type" in boxes.seen[0].text


def test_opening_an_unresolvable_attachment_says_so(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    window.openAttachment("nope")
    assert boxes.seen[0].text == (
        "This attachment is not available locally yet."
    )


def test_opening_a_broken_marker_warns(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_marker",
            "basename": "gone.bin",
            "rel_path": "",
        }
    )
    window.openAttachment(insert_log_row(payload))
    assert boxes.seen[0].text == "gone.bin has no stored location."


def test_a_sent_attachment_opens_without_a_prompt(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        katzen.QDesktopServices,
        "openUrl",
        staticmethod(appending_from(opened, local_file)),
    )
    source = tmp_path / "mine.txt"
    source.write_bytes(b"mine")
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_outgoing",
            "basename": "mine.txt",
            "filetype": "text/plain",
            "src_path": str(source),
        }
    )
    window.openAttachment(insert_log_row(payload, network_status=2))
    assert opened == [str(source)]
    assert boxes.seen == []


def test_saving_an_attachment_copies_it(
    window: katzen.MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, str]] = []
    dest = tmp_path / "copy.bin"

    def ask(
        parent: object,
        title: str,
        name: str,
        name_filter: str,
    ) -> tuple[str, str]:
        asked.append((name, name_filter))
        return str(dest), ""

    monkeypatch.setattr(
        katzen.QFileDialog,
        "getSaveFileName",
        staticmethod(ask),
    )
    window.saveAttachment(spill_received_marker("clip.opus", b"payload"))
    assert dest.read_bytes() == b"payload"
    assert asked == [("clip.opus", "")]


def test_saving_an_opus_attachment_offers_the_opus_filter(
    window: katzen.MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def ask(
        parent: object,
        title: str,
        name: str,
        name_filter: str,
    ) -> tuple[str, str]:
        asked.append(name_filter)
        return "", ""

    monkeypatch.setattr(
        katzen.QFileDialog,
        "getSaveFileName",
        staticmethod(ask),
    )
    rel_path = f"attachments/42/{uuid.uuid4().hex}-voice.opus"
    target = persistent.state_file.parent / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"opus")
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_marker",
            "basename": "voice.opus",
            "filetype": "audio/opus",
            "rel_path": rel_path,
            "sha256": hashlib.sha256(b"opus").digest(),
        }
    )
    window.saveAttachment(insert_log_row(payload))
    assert asked == ["Opus audio (*.opus)"]


def test_a_failing_save_is_reported(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        katzen.QFileDialog,
        "getSaveFileName",
        staticmethod(returning((str(tmp_path / "no-dir" / "x.bin"), ""))),
    )
    window.saveAttachment(spill_received_marker("clip.bin", b"payload"))
    assert boxes.seen[0].kind == "critical"
    assert boxes.seen[0].text.startswith("Could not save the attachment.")


def test_saving_an_unresolvable_attachment_says_so(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    window.saveAttachment("nope")
    assert boxes.seen[0].text == (
        "This attachment is not available locally yet."
    )


def test_saving_a_broken_marker_warns(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
) -> None:
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_marker",
            "basename": "gone.bin",
            "rel_path": "",
        }
    )
    window.saveAttachment(insert_log_row(payload))
    assert boxes.seen[0].text == "gone.bin has no stored location."


@pytest.mark.asyncio
async def test_push_to_talk_records_and_attaches_a_voice_note(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.push_to_talk_start()
    convo = loaded_window.convo_state()
    assert audio.started == [convo.conversation_id]
    assert loaded_window.push_to_talk_started is True
    assert loaded_window.push_to_talk_recording_conversation_id == (
        convo.conversation_id
    )
    assert loaded_window.ui.ptt_hold_space_label.text() == (
        "Recording audio... release Space to attach the voice note."
    )
    assert loaded_window.push_to_talk_watchdog.isActive() is True

    loaded_window.push_to_talk_finish(cancel=False)
    assert loaded_window.push_to_talk_started is False
    assert loaded_window.push_to_talk_watchdog.isActive() is False
    assert loaded_window.ui.ptt_hold_space_label.text() == (
        "Hold space bar to record audio."
    )
    assert len(convo.attached_files) == 1
    assert loaded_window.ui.singlemultitab.currentWidget() is (
        loaded_window.ui.attach_file_tab
    )
    assert (
        loaded_window.statusBar()
        .currentMessage()
        .startswith(
            "Voice note attached: ",
        )
    )


@pytest.mark.asyncio
async def test_cancelling_push_to_talk_keeps_nothing(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.push_to_talk_start()
    loaded_window.push_to_talk_finish(cancel=True)
    assert audio.cancels == 1
    assert loaded_window.convo_state().attached_files == set()


def test_finishing_without_a_recording_is_a_no_op(
    window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    window.push_to_talk_finish(cancel=False)
    assert audio.cancels == 0


@pytest.mark.asyncio
async def test_a_failing_capture_start_is_reported(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    audio.raise_on.add("start_capture")
    loaded_window.push_to_talk_start()
    assert loaded_window.push_to_talk_started is False
    assert boxes.seen[0].text == (
        "Failed to start push-to-talk capture.\n\nstart_capture failed"
    )


@pytest.mark.asyncio
async def test_a_failing_cancel_is_reported(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    loaded_window.push_to_talk_start()
    audio.raise_on.add("cancel_capture")
    loaded_window.push_to_talk_finish(cancel=True)
    assert boxes.seen[0].text == (
        "Failed to cancel push-to-talk capture.\n\ncancel_capture failed"
    )


@pytest.mark.asyncio
async def test_a_failing_finalize_is_reported(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    loaded_window.push_to_talk_start()
    audio.raise_on.add("stop_capture")
    loaded_window.push_to_talk_finish(cancel=False)
    assert boxes.seen[0].text == (
        "Failed to finalize push-to-talk capture.\n\nstop_capture failed"
    )
    assert loaded_window.convo_state().attached_files == set()


def test_finishing_without_an_engine_just_resets_the_ui(
    window: katzen.MainWindow,
) -> None:
    window.push_to_talk_started = True
    window.push_to_talk_finish(cancel=False)
    assert window.push_to_talk_started is False
    assert window.ui.ptt_hold_space_label.text() == (
        "Hold space bar to record audio."
    )


def test_the_watchdog_stops_itself_when_not_recording(
    window: katzen.MainWindow,
) -> None:
    window.push_to_talk_watchdog.start()
    window.push_to_talk_watchdog_tick()
    assert window.push_to_talk_watchdog.isActive() is False


def test_the_watchdog_cancels_when_no_conversation_is_selected(
    window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    window.push_to_talk_started = True
    window.push_to_talk_watchdog_tick()
    assert window.push_to_talk_started is False


@pytest.mark.asyncio
async def test_the_watchdog_cancels_when_the_conversation_changed(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.push_to_talk_start()
    loaded_window.push_to_talk_recording_conversation_id = 9999
    loaded_window.push_to_talk_watchdog_tick()
    assert loaded_window.push_to_talk_started is False
    assert audio.cancels == 1


@pytest.mark.asyncio
async def test_the_watchdog_cancels_when_the_ptt_tab_is_left(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.push_to_talk_start()
    loaded_window.ui.singlemultitab.setCurrentWidget(
        loaded_window.ui.attach_file_tab,
    )
    loaded_window.push_to_talk_watchdog_tick()
    assert loaded_window.push_to_talk_started is False
    assert audio.cancels == 1


@pytest.mark.asyncio
async def test_the_watchdog_finalizes_after_the_dead_mans_switch(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.ui.singlemultitab.setCurrentWidget(loaded_window.ui.ptt_tab)
    loaded_window.push_to_talk_start()
    convo = loaded_window.convo_state()
    convo.last_push_to_talk_ns = katzen.duration_time_ns() - 200_000_000
    loaded_window.push_to_talk_watchdog_tick()
    assert loaded_window.push_to_talk_started is False
    assert audio.cancels == 0
    assert len(convo.attached_files) == 1


@pytest.mark.asyncio
async def test_the_watchdog_keeps_recording_inside_the_window(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.ui.singlemultitab.setCurrentWidget(loaded_window.ui.ptt_tab)
    loaded_window.push_to_talk_start()
    loaded_window.convo_state().last_push_to_talk_ns = (
        katzen.duration_time_ns()
    )
    loaded_window.push_to_talk_watchdog_tick()
    assert loaded_window.push_to_talk_started is True


@pytest.mark.asyncio
async def test_a_press_outside_the_ptt_tab_stops_any_recording(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.ui.singlemultitab.setCurrentWidget(loaded_window.ui.ptt_tab)
    loaded_window.push_to_talk_start()
    loaded_window.ui.singlemultitab.setCurrentWidget(
        loaded_window.ui.singleline_tab,
    )
    assert loaded_window.push_to_talk_pressed() is True
    assert loaded_window.push_to_talk_started is False
    assert audio.cancels == 1


@pytest.mark.asyncio
async def test_the_first_press_only_arms_the_dead_mans_switch(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.ui.singlemultitab.setCurrentWidget(loaded_window.ui.ptt_tab)
    assert loaded_window.push_to_talk_pressed() is False
    assert loaded_window.push_to_talk_started is False
    assert loaded_window.convo_state().last_push_to_talk_ns > 0


@pytest.mark.asyncio
async def test_a_second_quick_press_starts_recording(
    loaded_window: katzen.MainWindow,
    audio: FakeAudio,
) -> None:
    loaded_window.ui.singlemultitab.setCurrentWidget(loaded_window.ui.ptt_tab)
    loaded_window.push_to_talk_pressed()
    assert loaded_window.push_to_talk_pressed() is False
    assert loaded_window.push_to_talk_started is True
    assert audio.started == [loaded_window.convo_state().conversation_id]


def test_the_key_press_stubs_run(
    window: katzen.MainWindow,
    qt_app: QApplication,
) -> None:
    from PySide6.QtGui import QKeyEvent

    event = QKeyEvent(
        katzen.QEvent.Type.KeyPress,
        Qt.Key.Key_Space,
        Qt.KeyboardModifier.NoModifier,
        " ",
    )
    window.X_keyPressEvent(event)
    window.X_keyReleaseEvent(event)
