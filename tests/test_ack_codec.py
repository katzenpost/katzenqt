"""The Acks field codec: the spec's naming examples, every malformed
field, and a round trip over random rosters."""

from __future__ import annotations

import cbor2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from katzenqt import ack_codec
from katzenqt.models import GroupChatMessage

_VALUE = 0xAA


@pytest.mark.parametrize(
    "indexes, naming, form, field_length",
    [
        ([9], "09", "list", 10),
        ([3, 12], "030c", "list", 19),
        ([3, 12, 13], "100c", "bitmap", 27),
        (list(range(1, 16)), "7fff", "bitmap", 123),
    ],
)
def test_the_examples_the_spec_gives(
    indexes: list[int], naming: str, form: str, field_length: int
) -> None:
    field = ack_codec.encode({index: _VALUE for index in indexes})
    assert len(field) == field_length
    assert field[0] == len(indexes) - 1
    width = len(field) - 1 - len(indexes) * ack_codec.VALUE_SIZE
    assert field[1 : 1 + width].hex() == naming
    assert (form == "list") == (width == len(indexes))
    assert list(ack_codec.decode(field)) == indexes


def test_a_value_is_an_idx64_little_endian() -> None:
    field = ack_codec.encode({0: 0x0102030405060708})
    assert field.hex() == "00" + "00" + "0807060504030201"


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
    field = bytes([values - 1]) + naming + bytes(8) * values
    with pytest.raises(ack_codec.AcksError, match=reason):
        ack_codec.decode(field)


def test_a_field_shorter_than_its_count_is_refused() -> None:
    with pytest.raises(ack_codec.AcksError, match="shorter than its count"):
        ack_codec.decode(bytes([1, 9]) + bytes(8))


@pytest.mark.parametrize(
    "acks, reason",
    [
        ({256: _VALUE}, "roster index out of range"),
        ({-1: _VALUE}, "roster index out of range"),
        ({3: -1}, "value out of range"),
        ({3: 1 << 64}, "value out of range"),
    ],
)
def test_what_cannot_be_written_is_refused(
    acks: dict[int, int], reason: str
) -> None:
    with pytest.raises(ack_codec.AcksError, match=reason):
        ack_codec.encode(acks)


_ACKS = st.dictionaries(
    st.integers(min_value=0, max_value=ack_codec.ROSTER_MAX - 1),
    st.integers(min_value=0, max_value=ack_codec.IDX64_MAX),
    max_size=ack_codec.ROSTER_MAX,
)


@settings(max_examples=300, deadline=None)
@given(acks=_ACKS)
def test_round_trip_over_any_roster(acks: dict[int, int]) -> None:
    field = ack_codec.encode(acks)
    assert ack_codec.decode(field) == dict(sorted(acks.items()))


@settings(max_examples=300, deadline=None)
@given(acks=_ACKS.filter(bool))
def test_the_shorter_form_is_always_the_one_written(
    acks: dict[int, int],
) -> None:
    field = ack_codec.encode(acks)
    width = len(field) - 1 - len(acks) * ack_codec.VALUE_SIZE
    assert width == min(len(acks), max(acks) // 8 + 1)
    assert width <= ack_codec.MAX_NAMING


@settings(max_examples=200, deadline=None)
@given(acks=_ACKS.filter(bool), text=st.text(max_size=20))
def test_the_field_survives_a_group_chat_message(
    acks: dict[int, int], text: str
) -> None:
    sent = GroupChatMessage(version=0, text=text, acks=ack_codec.encode(acks))
    received = GroupChatMessage.from_cbor(sent.to_cbor())
    assert received.acks is not None
    assert ack_codec.decode(received.acks) == dict(sorted(acks.items()))


def test_a_message_with_nothing_to_acknowledge_carries_no_field() -> None:
    sent = GroupChatMessage(version=0, text="hi")
    assert "acks" not in cbor2.loads(sent.to_cbor())
    assert GroupChatMessage.from_cbor(sent.to_cbor()).acks is None
