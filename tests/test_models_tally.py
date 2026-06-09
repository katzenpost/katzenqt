"""The tally message type rides the group-chat carrier intact.

Pins the explicit ``msg_type`` tag (integer on the wire, enum in memory), the
tally payload round-trip through ``to_cbor``/``from_cbor`` and through
``SendOperation.serialize``/``unserialize`` (including a multi-box CRDT blob),
and the backward-compatible inference for payloads written before ``msg_type``
existed.
"""
from __future__ import annotations

import uuid

import cbor2
import pytest

from katzenqt import models, persistent
from katzenqt.models import (
    GroupChatFileUpload,
    GroupChatMessage,
    GroupChatPleaseAdd,
    GroupChatTally,
    GroupChatTypeEnum,
    SendOperation,
)

MH = b"Z" * 32


def _substream_chunks(db_entries):
    return [
        e for e in db_entries
        if isinstance(e, persistent.PlaintextWAL) and e.indirection is None
    ]


def _to_chunks(pwals):
    return [(p.bacap_payload[:1], p.bacap_payload[1:]) for p in pwals]


def test_msg_type_is_an_integer_on_the_wire():
    gcm = GroupChatMessage(
        version=0, membership_hash=MH, msg_type=GroupChatTypeEnum.TALLY_VOTE,
        tally=GroupChatTally(survey_id=b"sid", choice={"s0": "yes"}),
    )
    raw = cbor2.loads(gcm.to_cbor())
    assert raw["msg_type"] == GroupChatTypeEnum.TALLY_VOTE.value
    assert isinstance(raw["msg_type"], int)


@pytest.mark.parametrize("kind", [
    GroupChatTypeEnum.TALLY_CREATE,
    GroupChatTypeEnum.TALLY_VOTE,
    GroupChatTypeEnum.TALLY_CLOSE,
    GroupChatTypeEnum.TALLY_SYNC_REQ,
    GroupChatTypeEnum.TALLY_SYNC_RESP,
])
def test_each_tally_kind_round_trips_through_cbor(kind):
    tally = GroupChatTally(
        survey_id=uuid.uuid4().bytes,
        version=3,
        choice={"s0": "yes", "s1": "maybe"} if kind is GroupChatTypeEnum.TALLY_VOTE else None,
        crdt=b"\x00\x01\x02\x03" if kind in (
            GroupChatTypeEnum.TALLY_CREATE,
            GroupChatTypeEnum.TALLY_SYNC_REQ,
            GroupChatTypeEnum.TALLY_SYNC_RESP,
        ) else None,
    )
    gcm = GroupChatMessage(version=0, membership_hash=MH, msg_type=kind, tally=tally)

    back = GroupChatMessage.from_cbor(gcm.to_cbor())
    assert back.msg_type is kind
    assert back.tally is not None
    assert back.tally.survey_id == tally.survey_id
    assert back.tally.version == 3
    assert back.tally.choice == tally.choice
    assert back.tally.crdt == tally.crdt


def test_large_crdt_blob_round_trips_through_send_operation():
    """A create blob too big for one box must chunk across boxes and reassemble."""
    blob = bytes((i * 7 + 3) & 0xFF for i in range(5000))
    gcm = GroupChatMessage(
        version=0, membership_hash=MH, msg_type=GroupChatTypeEnum.TALLY_CREATE,
        tally=GroupChatTally(survey_id=b"sid", crdt=blob),
    )
    op = SendOperation(bacap_stream=uuid.uuid4(), messages=[gcm])
    new_caps, db_entries = op.serialize(chunk_size=200, conversation_id=1)
    assert len(new_caps) == 1, "a multi-box send allocates an ephemeral stream"

    chunks = _to_chunks(_substream_chunks(db_entries))
    assert chunks[-1][0] == b"F"
    recovered = models.unserialize(chunks)
    assert recovered is not None
    assert recovered.msg_type is GroupChatTypeEnum.TALLY_CREATE
    assert recovered.tally.crdt == blob


def test_legacy_payload_without_msg_type_infers_text():
    legacy = cbor2.dumps({"version": 0, "membership_hash": MH, "text": "hi"})
    gcm = GroupChatMessage.from_cbor(legacy)
    assert gcm.msg_type is GroupChatTypeEnum.TEXT
    assert gcm.text == "hi"


def test_legacy_payload_without_msg_type_infers_file_upload():
    fu = GroupChatFileUpload(payload=b"abc", filetype="arbitrary", basename="a.bin")
    legacy = cbor2.dumps({
        "version": 0, "membership_hash": MH,
        "file_upload": fu.model_dump(),
    })
    gcm = GroupChatMessage.from_cbor(legacy)
    assert gcm.msg_type is GroupChatTypeEnum.FILE_UPLOAD
    assert gcm.file_upload.basename == "a.bin"


def test_legacy_payload_without_msg_type_infers_introduction():
    intro = GroupChatPleaseAdd(display_name="bob", read_cap=b"r" * 136)
    legacy = cbor2.dumps({
        "version": 0, "membership_hash": MH,
        "introduction": intro.model_dump(),
    })
    gcm = GroupChatMessage.from_cbor(legacy)
    assert gcm.msg_type is GroupChatTypeEnum.INTRODUCTION


def test_plain_text_message_defaults_to_text_type():
    gcm = GroupChatMessage(version=0, membership_hash=MH, text="hello")
    assert gcm.msg_type is GroupChatTypeEnum.TEXT
    assert GroupChatMessage.from_cbor(gcm.to_cbor()).msg_type is GroupChatTypeEnum.TEXT
