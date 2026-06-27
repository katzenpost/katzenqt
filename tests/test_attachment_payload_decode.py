"""Unit tests for ``qt_models._decode_group_chat_payload``.

These cover the renderer-facing decode of every ConversationLog.payload
shape the GUI can encounter: plain text, inline GroupChatMessage (text +
file upload), and the three CBOR attachment markers written by the network
layer / GUI send path (``file_marker``, ``file_oversized``, ``file_outgoing``).
"""
import cbor2

from katzenqt.models import GroupChatFileUpload, GroupChatMessage
from katzenqt.qt_models import _decode_group_chat_payload


def _marker(**fields) -> bytes:
    return b"F" + cbor2.dumps({"v": 0, **fields})


def test_plain_text_without_f_prefix():
    info = _decode_group_chat_payload(b"hello world")
    assert info.display == "hello world"
    assert info.kind == "text"
    assert info.is_audio is False
    assert info.basename is None
    assert info.rel_path is None


def test_inline_text_group_chat_message():
    gcm = GroupChatMessage(version=0, membership_hash=b"TODO" * 8, text="hi there")
    info = _decode_group_chat_payload(b"F" + gcm.to_cbor())
    assert info.display == "hi there"
    assert info.kind == "text"
    assert info.is_audio is False


def test_inline_file_upload_audio():
    upload = GroupChatFileUpload(
        payload=b"OggSfake", filetype="audio/opus", basename="note.opus",
    )
    gcm = GroupChatMessage(
        version=0, membership_hash=b"TODO" * 8, file_upload=upload,
    )
    info = _decode_group_chat_payload(b"F" + gcm.to_cbor())
    assert info.display == "Voice note: note.opus"
    assert info.kind == "inline"
    assert info.is_audio is True
    assert info.basename == "note.opus"
    assert info.filetype == "audio/opus"


def test_file_marker_non_audio():
    payload = _marker(
        kind="file_marker",
        basename="report.pdf",
        filetype="arbitrary",
        size=1234,
        rel_path="attachments/7/abc-report.pdf",
        sha256=b"\x00" * 32,
        membership_hash=b"TODO" * 8,
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == "[attachment] report.pdf"
    assert info.kind == "marker"
    assert info.is_audio is False
    assert info.rel_path == "attachments/7/abc-report.pdf"
    assert info.basename == "report.pdf"


def test_file_marker_audio():
    payload = _marker(
        kind="file_marker",
        basename="hello.opus",
        filetype="audio/opus",
        size=42,
        rel_path="attachments/7/def-hello.opus",
        sha256=b"\x11" * 32,
        membership_hash=b"TODO" * 8,
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == "Voice note: hello.opus"
    assert info.kind == "marker"
    assert info.is_audio is True
    assert info.rel_path == "attachments/7/def-hello.opus"


def test_file_oversized():
    payload = _marker(
        kind="file_oversized",
        basename="huge.bin",
        filetype="arbitrary",
        size=250 * 1024 * 1024,
        membership_hash=b"TODO" * 8,
    )
    info = _decode_group_chat_payload(payload)
    assert info.kind == "oversized"
    assert info.is_audio is False
    assert info.rel_path is None
    assert "too large" in info.display
    assert "250.0 MiB" in info.display


def test_file_outgoing_regular():
    payload = _marker(
        kind="file_outgoing",
        basename="report.pdf",
        filetype="arbitrary",
        size=9999,
        src_path="/home/me/report.pdf",
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == "[attachment] report.pdf"
    assert info.kind == "outgoing"
    assert info.is_audio is False
    # src_path is intentionally not exposed to QML:
    assert info.rel_path is None
    assert info.picture_path is None


def test_file_outgoing_audio_draft_empty_src_path():
    payload = _marker(
        kind="file_outgoing",
        basename="ptt.opus",
        filetype="audio/opus",
        size=512,
        src_path="",
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == "Voice note: ptt.opus"
    assert info.kind == "outgoing"
    assert info.is_audio is True
    assert info.rel_path is None


def test_file_marker_image_uses_thumbnail():
    payload = _marker(
        kind="file_marker",
        basename="photo.jpg",
        filetype="image/jpeg",
        size=1234,
        rel_path="attachments/7/abc-photo.jpg",
        thumb_rel_path="attachments/7/abc-thumb-photo.jpg.jpg",
        sha256=b"\x00" * 32,
        membership_hash=b"TODO" * 8,
    )
    info = _decode_group_chat_payload(payload)
    # Image rows render as a thumbnail, so the text line is suppressed.
    assert info.display == ""
    assert info.kind == "marker"
    assert info.is_audio is False
    assert info.picture_path == "attachments/7/abc-thumb-photo.jpg.jpg"
    assert info.rel_path == "attachments/7/abc-photo.jpg"


def test_file_marker_image_without_thumb_falls_back_to_full():
    payload = _marker(
        kind="file_marker",
        basename="diagram.png",
        filetype="arbitrary",  # legacy rows tagged generic; extension wins
        size=99,
        rel_path="attachments/7/def-diagram.png",
        sha256=b"\x11" * 32,
        membership_hash=b"TODO" * 8,
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == ""
    assert info.picture_path == "attachments/7/def-diagram.png"


def test_file_outgoing_image_uses_thumbnail():
    payload = _marker(
        kind="file_outgoing",
        basename="photo.jpg",
        filetype="image/jpeg",
        size=9999,
        src_path="/home/me/photo.jpg",
        thumb_rel_path="attachments/3/ghi-thumb-photo.jpg.jpg",
    )
    info = _decode_group_chat_payload(payload)
    assert info.display == ""
    assert info.kind == "outgoing"
    assert info.picture_path == "attachments/3/ghi-thumb-photo.jpg.jpg"
    # src_path is still not exposed to QML.
    assert info.rel_path is None
