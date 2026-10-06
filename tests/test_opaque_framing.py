from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from katzenqt.models import frame_opaque, unserialize_opaque

BLOBS = (b"", b"a", b"a sealed reply", bytes(range(256)) * 7)
SIZES = (2, 3, 7, 64, 1530)


@pytest.mark.parametrize("blob", BLOBS, ids=lambda b: str(len(b)))
@pytest.mark.parametrize("chunk_size", SIZES, ids=str)
def test_a_complete_run_round_trips(blob: bytes, chunk_size: int) -> None:
    assert unserialize_opaque(frame_opaque(blob, chunk_size)) == blob


@pytest.mark.parametrize("blob", BLOBS, ids=lambda b: str(len(b)))
@pytest.mark.parametrize("chunk_size", SIZES, ids=str)
def test_no_proper_prefix_of_a_run_yields_anything(
    blob: bytes, chunk_size: int,
) -> None:
    boxes = frame_opaque(blob, chunk_size)
    for stop in range(len(boxes)):
        assert unserialize_opaque(boxes[:stop]) is None


@pytest.mark.parametrize("blob", BLOBS, ids=lambda b: str(len(b)))
@pytest.mark.parametrize("chunk_size", SIZES, ids=str)
def test_every_box_fits_the_size_it_was_framed_for(
    blob: bytes, chunk_size: int,
) -> None:
    boxes = frame_opaque(blob, chunk_size)
    assert all(len(box) <= chunk_size for box in boxes)
    assert all(box[:1] == b"C" for box in boxes[:-1])
    assert boxes[-1][:1] == b"F"


def test_a_run_cannot_be_shortened_into_a_different_blob() -> None:
    boxes = frame_opaque(b"every member of the group", 8)
    assert len(boxes) > 2
    with pytest.raises(ValueError):
        unserialize_opaque(boxes[:1] + [b"F" + b"short"] + boxes[1:])


def test_the_marker_is_one_byte_of_the_budget() -> None:
    assert frame_opaque(b"x" * 6, 7) == [b"F" + b"x" * 6]
    assert frame_opaque(b"x" * 7, 7) == [b"C" + b"x" * 6, b"Fx"]


@given(
    blob=st.binary(max_size=4096),
    chunk_size=st.integers(min_value=2, max_value=512),
)
def test_a_run_is_whole_or_absent_for_any_blob(
    blob: bytes, chunk_size: int,
) -> None:
    boxes = frame_opaque(blob, chunk_size)
    assert unserialize_opaque(boxes) == blob
    assert all(unserialize_opaque(boxes[:n]) is None
               for n in range(len(boxes)))
