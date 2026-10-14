from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from katzenqt.models import unserialize_opaque

BLOBS = (b"", b"a", b"a sealed reply", bytes(range(256)) * 7)
SIZES = (2, 3, 7, 64, 1530)


def _boxes(blob: bytes, chunk_size: int) -> "list[bytes]":
    """The run of box payloads a blob of this size occupies: one marker
    byte per box, C for every box but the last and F for the last.

    Built here rather than imported, because nothing in katzenqt writes
    such a run yet; the reader is what this file is about.
    """
    body = chunk_size - 1
    if len(blob) <= body:
        return [b"F" + blob]
    out = [
        b"C" + blob[off:off + body]
        for off in range(0, len(blob) - body, body)
    ]
    return out + [b"F" + blob[len(out) * body:]]


@pytest.mark.parametrize("blob", BLOBS, ids=lambda b: str(len(b)))
@pytest.mark.parametrize("chunk_size", SIZES, ids=str)
def test_a_complete_run_round_trips(blob: bytes, chunk_size: int) -> None:
    assert unserialize_opaque(_boxes(blob, chunk_size)) == blob


@pytest.mark.parametrize("blob", BLOBS, ids=lambda b: str(len(b)))
@pytest.mark.parametrize("chunk_size", SIZES, ids=str)
def test_no_proper_prefix_of_a_run_yields_anything(
    blob: bytes, chunk_size: int,
) -> None:
    boxes = _boxes(blob, chunk_size)
    for stop in range(len(boxes)):
        assert unserialize_opaque(boxes[:stop]) is None


def test_a_run_cannot_be_shortened_into_a_different_blob() -> None:
    boxes = _boxes(b"every member of the group", 8)
    assert len(boxes) > 2
    with pytest.raises(ValueError):
        unserialize_opaque(boxes[:1] + [b"F" + b"short"] + boxes[1:])


@given(
    blob=st.binary(max_size=4096),
    chunk_size=st.integers(min_value=2, max_value=512),
)
def test_a_run_is_whole_or_absent_for_any_blob(
    blob: bytes, chunk_size: int,
) -> None:
    boxes = _boxes(blob, chunk_size)
    assert unserialize_opaque(boxes) == blob
    assert all(unserialize_opaque(boxes[:n]) is None
               for n in range(len(boxes)))
