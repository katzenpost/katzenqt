"""Tests for katzenqt.katzen_util helpers."""
import struct

from katzenqt.katzen_util import bacap_idx64


def test_bacap_idx64_reads_little_endian_counter():
    # 104 bytes total; first 8 are the LE u64 counter.
    blob = struct.pack("<Q", 42) + b"\x00" * 96
    assert bacap_idx64(blob) == 42


def test_bacap_idx64_handles_full_range():
    # High bit set still fits in a Python int (no signed interpretation).
    idx = (1 << 62) + 17
    blob = struct.pack("<Q", idx) + b"\x00" * 96
    assert bacap_idx64(blob) == idx


def test_bacap_idx64_ignores_trailing_bytes():
    # BACAP appends CurBlindingFactor + CurEncryptionKey + HKDFState after
    # the counter; those MUST NOT bleed into the result.
    blob = struct.pack("<Q", 7) + b"\xff" * 96
    assert bacap_idx64(blob) == 7


def test_bacap_idx64_ordering_is_integer_not_bytewise():
    # Regression against reaching for bytewise comparison: 0x00ff
    # lex-compares below 0x0100, but numerically the order reverses.
    lo = struct.pack("<Q", 0x00FF) + b"\x00" * 96
    hi = struct.pack("<Q", 0x0100) + b"\x00" * 96
    assert bacap_idx64(hi) > bacap_idx64(lo)
    assert hi < lo  # bytewise (raw blob) says the opposite
