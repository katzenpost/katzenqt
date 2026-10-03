"""The Acks field codec: the spec's examples, every malformed field, a
round trip over random rosters, and the vectors shared with the Go and Rust
codecs."""

from __future__ import annotations

import json
import pathlib
from typing import TypedDict

import cbor2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from katzenqt import ack_codec
from katzenqt.models import GroupChatMessage


class _Case(TypedDict):
    name: str
    value_size: int
    roster_size: int
    acks: list[tuple[int, str]]
    cbor: str


_VECTORS: list[_Case] = json.loads(
    (pathlib.Path(__file__).parent / "data" / "acks_vectors.json").read_text()
)["cases"]

_VALUE = bytes([0xAA]) * ack_codec.VALUE_SIZE


@pytest.mark.parametrize(
    "indexes, naming, form, field_length",
    [
        ([9], "09", "list", 105),
        ([3, 12], "030c", "list", 210),
        ([3, 12, 13], "100c", "bitmap", 314),
        (list(range(1, 16)), "7fff", "bitmap", 1562),
    ],
)
def test_the_examples_the_spec_gives(
    indexes: list[int], naming: str, form: str, field_length: int
) -> None:
    field = ack_codec.encode({index: _VALUE for index in indexes})
    assert len(field) == field_length
    width = len(field) - len(indexes) * ack_codec.VALUE_SIZE
    assert field[:width].hex() == naming
    assert (form == "list") == (width == len(indexes))
    assert list(ack_codec.decode(field)) == indexes


@pytest.mark.parametrize("case", _VECTORS, ids=lambda case: case["name"])
def test_vectors_shared_with_the_go_and_rust_codecs(case: _Case) -> None:
    acks = {index: bytes.fromhex(value) for index, value in case["acks"]}
    field = ack_codec.encode(acks, case["value_size"])
    assert cbor2.dumps(field).hex() == case["cbor"]
    assert ack_codec.decode(field, case["value_size"]) == dict(
        sorted(acks.items())
    )


def test_the_vectors_cover_both_forms_and_the_last_roster_index() -> None:
    forms = set()
    for case in _VECTORS:
        field = cbor2.loads(bytes.fromhex(case["cbor"]))
        count, width = divmod(len(field), case["value_size"])
        forms.add("list" if width == count else "bitmap")
    assert forms == {"list", "bitmap"}
    assert any(index == 255 for case in _VECTORS for index, _ in case["acks"])


@pytest.mark.parametrize(
    "naming, values, reason",
    [
        (bytes([1, 2, 3]), 1, "more naming bytes than values"),
        (bytes([5, 3]), 2, "list is not in ascending order"),
        (bytes([3, 3]), 2, "list is not in ascending order"),
        (bytes([0x70, 0x00]), 3, "bitmap ends in a zero byte"),
        (bytes([0xF0]), 3, "bitmap does not name one member per value"),
        (bytes([0x80]), 3, "bitmap does not name one member per value"),
        (b"", 2, "bitmap ends in a zero byte"),
        (bytes([0x01]) * 33, 34, "bitmap is longer than a roster"),
    ],
)
def test_a_malformed_field_is_refused(
    naming: bytes, values: int, reason: str
) -> None:
    with pytest.raises(ack_codec.AcksError, match=reason):
        ack_codec.decode(naming + _VALUE * values)


def test_a_value_no_longer_than_the_longest_bitmap_is_refused() -> None:
    short = ack_codec.MIN_VALUE_SIZE - 1
    with pytest.raises(ack_codec.AcksError, match="value too short"):
        ack_codec.decode(b"\x09" + bytes(short), short)
    with pytest.raises(ack_codec.AcksError, match="value too short"):
        ack_codec.encode({9: bytes(short)}, short)


@pytest.mark.parametrize(
    "acks, reason",
    [
        ({256: _VALUE}, "roster index out of range"),
        ({-1: _VALUE}, "roster index out of range"),
        ({3: _VALUE[:-1]}, "value of the wrong size"),
    ],
)
def test_what_cannot_be_written_is_refused(
    acks: dict[int, bytes], reason: str
) -> None:
    with pytest.raises(ack_codec.AcksError, match=reason):
        ack_codec.encode(acks)


_ACKS = st.dictionaries(
    st.integers(min_value=0, max_value=ack_codec.ROSTER_MAX - 1),
    st.binary(min_size=ack_codec.VALUE_SIZE, max_size=ack_codec.VALUE_SIZE),
    max_size=ack_codec.ROSTER_MAX,
)


@settings(max_examples=300, deadline=None)
@given(acks=_ACKS)
def test_round_trip_over_any_roster(acks: dict[int, bytes]) -> None:
    field = ack_codec.encode(acks)
    assert ack_codec.decode(field) == dict(sorted(acks.items()))


@settings(max_examples=300, deadline=None)
@given(acks=_ACKS)
def test_the_shorter_form_is_always_the_one_written(
    acks: dict[int, bytes],
) -> None:
    field = ack_codec.encode(acks)
    width = len(field) - len(acks) * ack_codec.VALUE_SIZE
    bitmap_width = max(acks) // 8 + 1 if acks else 0
    assert width == min(len(acks), bitmap_width)
    assert width <= ack_codec.MAX_NAMING


@settings(max_examples=200, deadline=None)
@given(acks=_ACKS.filter(bool), text=st.text(max_size=20))
def test_the_field_survives_a_group_chat_message(
    acks: dict[int, bytes], text: str
) -> None:
    sent = GroupChatMessage(version=0, text=text, acks=ack_codec.encode(acks))
    received = GroupChatMessage.from_cbor(sent.to_cbor())
    assert received.acks is not None
    assert ack_codec.decode(received.acks) == dict(sorted(acks.items()))


def test_a_message_with_nothing_to_acknowledge_carries_no_field() -> None:
    sent = GroupChatMessage(version=0, text="hi")
    assert "acks" not in cbor2.loads(sent.to_cbor())
    assert GroupChatMessage.from_cbor(sent.to_cbor()).acks is None
