import uuid

import pytest

from katzenqt import models

CAP_A = bytes([0x11]) * 136
CAP_B = bytes([0x22]) * 136


def _please_add(name: str, cap: bytes) -> models.GroupChatPleaseAdd:
    return models.GroupChatPleaseAdd(display_name=name, read_cap=cap)


def test_reply_who_round_trips_through_cbor() -> None:
    original = models.GroupChatReplyWho(
        please_adds=[_please_add("alice", CAP_A), _please_add("bob", CAP_B)],
    )
    restored = models.GroupChatReplyWho.from_cbor(original.to_cbor())
    listed = [p for p in restored.please_adds if p is not None]
    assert [p.display_name for p in listed] == ["alice", "bob"]
    assert [p.read_cap for p in listed] == [CAP_A, CAP_B]


@pytest.mark.parametrize("chunk_size", [-1, 0, 1])
def test_serialize_refuses_a_chunk_size_that_cannot_hold_a_prefix(
    chunk_size: int,
) -> None:
    op = models.SendOperation(
        bacap_stream=uuid.uuid4(),
        messages=[
            models.GroupChatMessage(
                version=0,
                text="hi",
            )
        ],
    )
    with pytest.raises(Exception, match="max payload size"):
        op.serialize(chunk_size, 1)


def test_as_introduction_returns_the_payload_for_an_introduction() -> None:
    intro = _please_add("carol", CAP_A)
    gcm = models.GroupChatMessage(
        version=0,
        introduction=intro,
    )
    assert gcm.msg_type == models.GroupChatTypeEnum.INTRODUCTION
    assert gcm.as_introduction is intro


def test_as_introduction_is_none_for_a_plain_text_message() -> None:
    gcm = models.GroupChatMessage(
        version=0,
        text="hello",
    )
    assert gcm.as_introduction is None
