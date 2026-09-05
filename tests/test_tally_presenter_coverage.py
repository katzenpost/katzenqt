from __future__ import annotations

import uuid

from katzenqt import persistent
from katzenqt.models import GroupChatMessage, GroupChatTally, GroupChatTypeEnum
from katzenqt.tally import engine, presenter, schema, sync
from katzenqt.tally.controller import voter_id_from_read_cap
from katzenqt.tally.schema import Mode

ALICE_CAP = bytes([0x02]) * 136
OWN_CAP = bytes([0x01]) * 136


def _summary(survey_id: bytes) -> presenter.SurveySummary:
    doc = schema.new_survey_doc(survey_id, "lunch?", Mode.APPROVAL, ["a", "b"])
    return presenter.summarize(doc, conversation_id=1)


def _message(
    kind: GroupChatTypeEnum, survey_id: bytes, *,
    choice: "dict[str, str] | None" = None, version: int = 0,
) -> GroupChatMessage:
    return GroupChatMessage(
        version=0, membership_hash=bytes(32), msg_type=kind,
        tally=GroupChatTally(
            survey_id=survey_id, choice=choice, version=version,
        ),
    )


def _conversation(
    name: str = "g", *,
    own_cap: "bytes | None" = OWN_CAP,
    peer_cap: "bytes | None" = ALICE_CAP,
    peer_active: bool = True,
) -> int:
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcap.id, read_cap=own_cap,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=own_rcap.id, conversation=convo,
    )
    convo.own_peer = own_peer
    peer_rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=peer_cap)
    peer = persistent.ConversationPeer(
        name="alice", read_cap_id=peer_rcap.id, active=peer_active,
        conversation=convo,
    )
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        sess.add(peer_rcap)
        sess.add(peer)
        sess.commit()
        conversation_id: int = convo.id
        return conversation_id


def test_a_voter_row_without_a_vote_renders_as_not_voted() -> None:
    doc = schema.new_survey_doc(
        uuid.uuid4().bytes, "lunch?", Mode.APPROVAL, ["a"],
    )
    summary = presenter.summarize(doc, conversation_id=1)
    row = presenter.VoterRow(name="alice", choices={}, has_voted=False)
    assert row.line(summary.slots) == "alice: hasn't voted"


def test_a_voter_row_with_no_marked_slots_says_so() -> None:
    doc = schema.new_survey_doc(
        uuid.uuid4().bytes, "lunch?", Mode.APPROVAL, ["a"],
    )
    summary = presenter.summarize(doc, conversation_id=1)
    row = presenter.VoterRow(name="alice", choices={}, has_voted=True)
    assert row.line(summary.slots) == "alice: no selections"


def test_a_message_without_a_tally_payload_renders_as_malformed() -> None:
    gcm = GroupChatMessage(version=0, membership_hash=bytes(32), text="hi")
    row = presenter.tally_row_text(gcm, actor_name="alice", survey_summary=None)
    assert row.kind == "invalid"
    assert row.text == "alice: malformed tally message"
    assert row.survey_id is None


def test_a_create_we_could_not_read_renders_as_invalid() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_CREATE, survey_id),
        actor_name="alice", survey_summary=None,
    )
    assert row.kind == "invalid"
    assert row.text == "alice: could not read the new poll"
    assert row.survey_id == survey_id


def test_a_vote_for_an_unknown_poll_renders_as_invalid() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_VOTE, survey_id, choice={"s0": "yes"}),
        actor_name="alice", survey_summary=None,
    )
    assert row.kind == "invalid"
    assert survey_id.hex() in row.text


def test_a_close_names_the_poll_it_closed() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_CLOSE, survey_id),
        actor_name="alice", survey_summary=_summary(survey_id),
    )
    assert row.kind == "close"
    assert row.text == 'alice closed "[Poll] lunch?"'


def test_a_close_for_an_unknown_poll_renders_as_invalid() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_CLOSE, survey_id),
        actor_name="alice", survey_summary=None,
    )
    assert row.kind == "invalid"
    assert survey_id.hex() in row.text


def test_a_sync_row_uses_the_topic_when_the_poll_is_known() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_SYNC_REQ, survey_id),
        actor_name="alice", survey_summary=_summary(survey_id),
    )
    assert row.kind == "sync"
    assert row.text == 'alice synced the poll "[Poll] lunch?"'


def test_a_sync_row_falls_back_to_the_survey_id() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TALLY_SYNC_RESP, survey_id),
        actor_name="alice", survey_summary=None,
    )
    assert row.kind == "sync"
    assert survey_id.hex() in row.text


def test_an_unsupported_tally_kind_renders_as_invalid() -> None:
    survey_id = uuid.uuid4().bytes
    row = presenter.tally_row_text(
        _message(GroupChatTypeEnum.TEXT, survey_id),
        actor_name="alice", survey_summary=None,
    )
    assert row.kind == "invalid"
    assert row.text == "alice: unsupported tally message"


def test_own_voter_id_is_none_for_an_unknown_conversation() -> None:
    assert presenter.own_voter_id(0xFFFFFF) is None


def test_own_voter_id_is_none_when_the_own_peer_row_is_gone() -> None:
    convo_id = _conversation()
    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, convo_id)
        assert conv is not None
        own = sess.get(persistent.ConversationPeer, conv.own_peer_id)
        assert own is not None
        sess.delete(own)
        sess.commit()
    assert presenter.own_voter_id(convo_id) is None


def test_own_voter_id_is_none_without_a_provisioned_read_cap() -> None:
    convo_id = _conversation(own_cap=None)
    assert presenter.own_voter_id(convo_id) is None


def test_voter_names_is_empty_for_an_unknown_conversation() -> None:
    assert presenter.voter_names(0xFFFFFF) == {}


def test_voter_names_skips_paused_peers_and_unprovisioned_caps() -> None:
    paused = _conversation("paused", peer_active=False)
    assert voter_id_from_read_cap(ALICE_CAP) not in presenter.voter_names(paused)

    unprovisioned = _conversation("unprovisioned", peer_cap=None)
    names = presenter.voter_names(unprovisioned)
    assert list(names.values()) == ["me"]


def test_new_poll_count_is_zero_for_an_unknown_conversation() -> None:
    assert presenter.new_poll_count(0xFFFFFF) == 0


def test_new_poll_count_skips_unframed_and_undecodable_rows() -> None:
    convo_id = _conversation()
    survey_id = uuid.uuid4().bytes
    doc = schema.new_survey_doc(survey_id, "lunch?", Mode.APPROVAL, ["a"])
    create = _message(GroupChatTypeEnum.TALLY_CREATE, survey_id)
    create.tally = GroupChatTally(survey_id=survey_id, crdt=sync.full_state(doc))
    payloads = [
        b"C" + create.to_cbor(),
        b"F" + b"not cbor at all",
        b"F" + create.to_cbor(),
    ]
    with persistent.Session(persistent._engine_sync) as sess:
        conv = sess.get(persistent.Conversation, convo_id)
        assert conv is not None
        for order, payload in enumerate(payloads):
            sess.add(persistent.ConversationLog(
                conversation_id=convo_id, conversation_peer_id=conv.own_peer_id,
                conversation_order=order, payload=payload,
            ))
        conv.first_unread = 0
        sess.add(conv)
        sess.commit()

    assert presenter.new_poll_count(convo_id) == 1


def test_conversation_ids_lists_every_conversation() -> None:
    first = _conversation("one")
    second = _conversation("two")
    assert sorted(presenter.conversation_ids()) == sorted([first, second])


def test_summarize_keeps_my_choices_when_another_voter_leads() -> None:
    doc = schema.new_survey_doc(
        uuid.uuid4().bytes, "lunch?", Mode.APPROVAL, ["a", "b"],
    )
    mine = voter_id_from_read_cap(OWN_CAP)
    engine.apply_vote(doc, voter_id_from_read_cap(ALICE_CAP), {"s0": "yes"})
    engine.apply_vote(doc, mine, {"s1": "no"})
    summary = presenter.summarize(doc, conversation_id=1, my_voter_id=mine)
    assert summary.my_choices == {"s1": "no"}
