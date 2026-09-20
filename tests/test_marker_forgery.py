"""Regression tests for the attachment-marker forgery fix.

A peer's GroupChatMessage is CBOR; ``GroupChatMessage.from_cbor`` ignores
unknown keys, so a hostile peer can smuggle attachment-marker keys ("kind",
"src_path", "rel_path") alongside an ordinary text field. If the raw wire
bytes are persisted, the render path reads them back as a locally-authored
attachment marker and offers Open/Save on an arbitrary local file. The receive
path now re-serialises the validated model (``to_cbor``), which emits only the
model's own fields and drops the smuggled keys.
"""
import cbor2

from katzenqt.models import GroupChatMessage
from katzenqt.qt_models import _decode_group_chat_payload


_HOSTILE = {
    "version": 0,
    "membership_hash": b"\x00" * 32,
    "msg_type": 0,
    "text": "hi",
    "kind": "file_outgoing",
    "basename": "cat.jpg",
    "src_path": "/home/user/.ssh/id_ed25519",
    "rel_path": "katzen.sqlite3",
    "sha256": b"\x11" * 32,
}


def test_reserialization_drops_smuggled_marker_keys() -> None:
    gcm = GroupChatMessage.from_cbor(cbor2.dumps(_HOSTILE))
    decoded = cbor2.loads(gcm.to_cbor())
    assert "kind" not in decoded
    assert "src_path" not in decoded
    assert "rel_path" not in decoded
    assert decoded["text"] == "hi"


def test_raw_forged_bytes_would_decode_as_a_marker() -> None:
    raw = b"F" + cbor2.dumps(_HOSTILE)
    info = _decode_group_chat_payload(raw)
    assert info.kind in ("outgoing", "marker", "oversized")


def test_reserialized_bytes_decode_as_text() -> None:
    gcm = GroupChatMessage.from_cbor(cbor2.dumps(_HOSTILE))
    info = _decode_group_chat_payload(b"F" + gcm.to_cbor())
    assert info.kind == "text"
    assert info.display == "hi"


def test_reserialization_is_byte_identical_for_honest_traffic() -> None:
    legit = GroupChatMessage(version=0, membership_hash=b"\x00" * 32, text="hello")
    once = legit.to_cbor()
    assert GroupChatMessage.from_cbor(once).to_cbor() == once
