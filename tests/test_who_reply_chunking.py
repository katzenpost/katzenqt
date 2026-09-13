"""Bounded voucher reply framing and reassembly tests."""
from __future__ import annotations

import types

import pytest

from katzenqt import voucher
from katzenqt.voucher import (
    MAX_BOX_PLAINTEXT,
    MAX_WHO_REPLY_CHUNKS,
    WHO_REPLY_CHUNK_MAGIC,
    WhoReplyTooLargeError,
    _chunk_sealed_reply,
    _who_reply_chunk_count,
)


def test_small_reply_is_one_raw_box() -> None:
    sealed = b"s" * 1000
    chunks = _chunk_sealed_reply(sealed)
    assert chunks == [sealed]
    assert _who_reply_chunk_count(chunks[0]) is None


def test_boundary_at_one_box() -> None:
    assert _chunk_sealed_reply(b"a" * MAX_BOX_PLAINTEXT) == [
        b"a" * MAX_BOX_PLAINTEXT]
    over = _chunk_sealed_reply(b"a" * (MAX_BOX_PLAINTEXT + 1))
    assert _who_reply_chunk_count(over[0]) == 2


def test_manifest_header_is_exact_wire_bytes() -> None:
    chunks = _chunk_sealed_reply(b"a" * (2 * MAX_BOX_PLAINTEXT + 1))
    assert chunks[0] == b"KPWR1\x00\x03"


def test_large_reply_manifest_then_slices() -> None:
    sealed = b"".join(bytes([i % 256]) for i in range(4000))
    chunks = _chunk_sealed_reply(sealed)
    count = _who_reply_chunk_count(chunks[0])
    assert count == 3
    assert len(chunks) == 1 + count
    assert all(len(s) <= MAX_BOX_PLAINTEXT for s in chunks[1:])
    assert b"".join(chunks[1:]) == sealed


def test_manifest_detection_not_fooled_by_a_long_magic_prefixed_reply() -> None:
    sealed = WHO_REPLY_CHUNK_MAGIC + b"z" * 500
    assert _who_reply_chunk_count(sealed) is None


def test_too_large_to_chunk_raises() -> None:
    huge = b"q" * (MAX_BOX_PLAINTEXT * (MAX_WHO_REPLY_CHUNKS + 1))
    with pytest.raises(WhoReplyTooLargeError):
        _chunk_sealed_reply(huge)


class _FakeVoucherStream:
    """A single BACAP stream: encrypt_write stores a box, encrypt_read returns
    the next index, and the read-path start_resending returns the stored box.
    Box indices are an incrementing 8-byte counter."""

    def __init__(self) -> None:
        self.boxes: dict[bytes, bytes] = {}

    @staticmethod
    def _next(index: bytes) -> bytes:
        return (int.from_bytes(index, "big") + 1).to_bytes(len(index), "big")

    async def encrypt_write(self, *, plaintext, write_cap, message_box_index):
        self.boxes[message_box_index] = plaintext
        return types.SimpleNamespace(
            envelope_descriptor=b"", message_ciphertext=b"", envelope_hash=b"",
            next_message_box_index=self._next(message_box_index))

    async def encrypt_read(self, *, read_cap, message_box_index):
        return types.SimpleNamespace(
            envelope_descriptor=b"", message_ciphertext=b"", envelope_hash=b"",
            next_message_box_index=self._next(message_box_index))

    async def start_resending_encrypted_message(
        self, *, read_cap=None, write_cap=None, message_box_index=None,
        reply_index=None, envelope_descriptor=b"", message_ciphertext=b"",
        envelope_hash=b"", no_retry_on_box_id_not_found=False,
    ):
        return types.SimpleNamespace(
            plaintext=self.boxes.get(message_box_index, b""))


async def _roundtrip(sealed: bytes) -> bytes:
    conn = _FakeVoucherStream()
    box1 = (7).to_bytes(8, "big")
    await voucher._publish_sealed_reply(conn, b"w" * 168, box1, sealed)
    return await voucher._read_sealed_reply(conn, b"r" * 136, box1)


@pytest.mark.asyncio
async def test_roundtrip_single_box() -> None:
    sealed = b"one-box reply" * 20
    assert len(sealed) <= MAX_BOX_PLAINTEXT
    assert await _roundtrip(sealed) == sealed


@pytest.mark.asyncio
async def test_roundtrip_multi_box() -> None:
    sealed = bytes((i * 7) % 256 for i in range(5000))
    assert await _roundtrip(sealed) == sealed


@pytest.mark.parametrize("count", [0, 1, 65, 65535])
def test_rejects_unbounded_manifest(count: int) -> None:
    with pytest.raises(ValueError, match="chunk count"):
        _who_reply_chunk_count(
            WHO_REPLY_CHUNK_MAGIC + count.to_bytes(2, "big", signed=False)
        )
