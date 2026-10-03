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
    assert [p.display_name for p in restored.please_adds] == ["alice", "bob"]
    assert [p.read_cap for p in restored.please_adds] == [CAP_A, CAP_B]


def test_reply_who_membership_hash_is_32_bytes() -> None:
    reply = models.GroupChatReplyWho(please_adds=[_please_add("alice", CAP_A)])
    assert len(reply.membership_hash()) == 32


def test_reply_who_membership_hash_is_stable_for_equal_content() -> None:
    one = models.GroupChatReplyWho(please_adds=[_please_add("alice", CAP_A)])
    two = models.GroupChatReplyWho(please_adds=[_please_add("alice", CAP_A)])
    assert one.membership_hash() == two.membership_hash()


def test_reply_who_membership_hash_tracks_the_member_set() -> None:
    one = models.GroupChatReplyWho(please_adds=[_please_add("alice", CAP_A)])
    two = models.GroupChatReplyWho(please_adds=[_please_add("alice", CAP_B)])
    assert one.membership_hash() != two.membership_hash()


def test_reply_who_membership_hash_depends_on_order() -> None:
    a = _please_add("alice", CAP_A)
    b = _please_add("bob", CAP_B)
    assert (
        models.GroupChatReplyWho(please_adds=[a, b]).membership_hash()
        != models.GroupChatReplyWho(please_adds=[b, a]).membership_hash()
    )


@pytest.mark.parametrize("chunk_size", [-1, 0, 1])
def test_serialize_refuses_a_chunk_size_that_cannot_hold_a_prefix(
    chunk_size: int,
) -> None:
    op = models.SendOperation(
        bacap_stream=uuid.uuid4(),
        messages=[
            models.GroupChatMessage(
                version=0,
                membership_hash=bytes(32),
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
        membership_hash=bytes(32),
        introduction=intro,
    )
    assert gcm.msg_type == models.GroupChatTypeEnum.INTRODUCTION
    assert gcm.as_introduction is intro


def test_as_introduction_is_none_for_a_plain_text_message() -> None:
    gcm = models.GroupChatMessage(
        version=0,
        membership_hash=bytes(32),
        text="hello",
    )
    assert gcm.as_introduction is None
