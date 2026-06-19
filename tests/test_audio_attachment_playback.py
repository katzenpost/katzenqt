"""Tests for ``MainWindow._resolve_attachment`` — the on-demand rehydration
of a ConversationLog payload into a concrete on-disk path used by the QML
play/open/save slots.

The method does not touch ``self``, so it is exercised here against a bare
``types.SimpleNamespace`` stand-in rather than constructing a real Qt window
(which would require a display / PySide6 widgets).
"""
import hashlib
import types
import uuid

import cbor2
import pytest

from katzenqt import persistent
from katzenqt.katzen import MainWindow, _AttachmentError, _ResolvedAttachment


def _insert_payload(payload: bytes) -> str:
    """Persist a ConversationLog row carrying ``payload`` and return its
    string message id. FK columns point at synthetic ids; SQLite does not
    enforce the constraints in the test schema."""
    message_uuid = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            id=message_uuid,
            conversation_id=1,
            conversation_peer_id=1,
            conversation_order=0,
            payload=payload,
        ))
        sess.commit()
    return str(message_uuid)


def _resolve(message_id: str):
    return MainWindow._resolve_attachment(types.SimpleNamespace(), message_id)


def test_file_marker_resolves_to_spilled_file(tmp_path):
    blob = b"received attachment bytes"
    conv_dir = persistent.state_file.parent / "attachments" / "9"
    conv_dir.mkdir(parents=True, exist_ok=True)
    rel_path = "attachments/9/aaa-doc.pdf"
    (persistent.state_file.parent / rel_path).write_bytes(blob)

    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_marker",
        "basename": "doc.pdf",
        "filetype": "arbitrary",
        "size": len(blob),
        "rel_path": rel_path,
        "sha256": hashlib.sha256(blob).digest(),
        "membership_hash": b"TODO" * 8,
    })
    message_id = _insert_payload(payload)

    resolved = _resolve(message_id)
    assert isinstance(resolved, _ResolvedAttachment)
    assert resolved.basename == "doc.pdf"
    assert resolved.filetype == "arbitrary"
    assert resolved.path.read_bytes() == blob


def test_file_marker_checksum_mismatch_raises():
    rel_path = "attachments/9/bbb-tampered.bin"
    abs_path = persistent.state_file.parent / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"actual on-disk bytes")

    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_marker",
        "basename": "tampered.bin",
        "filetype": "arbitrary",
        "size": 10,
        "rel_path": rel_path,
        "sha256": hashlib.sha256(b"different bytes").digest(),
        "membership_hash": b"TODO" * 8,
    })
    message_id = _insert_payload(payload)

    with pytest.raises(_AttachmentError):
        _resolve(message_id)


def test_file_marker_missing_file_raises():
    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_marker",
        "basename": "gone.bin",
        "filetype": "arbitrary",
        "size": 10,
        "rel_path": "attachments/9/does-not-exist.bin",
        "sha256": hashlib.sha256(b"x").digest(),
        "membership_hash": b"TODO" * 8,
    })
    message_id = _insert_payload(payload)

    with pytest.raises(_AttachmentError):
        _resolve(message_id)


def test_file_oversized_raises():
    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_oversized",
        "basename": "huge.bin",
        "filetype": "arbitrary",
        "size": 250 * 1024 * 1024,
        "membership_hash": b"TODO" * 8,
    })
    message_id = _insert_payload(payload)

    with pytest.raises(_AttachmentError):
        _resolve(message_id)


def test_file_outgoing_resolves_to_src_path(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"jpeg bytes")

    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_outgoing",
        "basename": "photo.jpg",
        "filetype": "arbitrary",
        "size": 10,
        "src_path": str(src),
    })
    message_id = _insert_payload(payload)

    resolved = _resolve(message_id)
    assert isinstance(resolved, _ResolvedAttachment)
    assert resolved.path == src
    assert resolved.path.read_bytes() == b"jpeg bytes"


def test_file_outgoing_empty_src_path_returns_none():
    payload = b"F" + cbor2.dumps({
        "v": 0,
        "kind": "file_outgoing",
        "basename": "ptt.opus",
        "filetype": "audio/opus",
        "size": 512,
        "src_path": "",
    })
    message_id = _insert_payload(payload)

    assert _resolve(message_id) is None
