from __future__ import annotations

import logging
import uuid

import pytest
from pycrdt import Doc
from sqlmodel import select

from katzenqt import persistent
from katzenqt.models import (
    GroupChatMessage,
    GroupChatTally,
    GroupChatTypeEnum,
)
from katzenqt.tally import engine, events, schema, send, sync
from katzenqt.tally.controller import TallyController, voter_id_from_read_cap
from katzenqt.tally.schema import Mode

ALICE_CAP = bytes([0x02]) * 136
BOB_CAP = bytes([0x03]) * 136
CAROL_CAP = bytes([0x05]) * 136
OWN_CAP = bytes([0x01]) * 136


async def _make_convo(
    sess: persistent.AsyncSession,
    name: str,
    own_cap: "bytes | None",
    peer_caps: "dict[str, bytes | None]",
) -> "tuple[persistent.Conversation, dict[str, persistent.ConversationPeer]]":
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        write_cap_id=wcap.id,
        read_cap=own_cap,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(
        name="me",
        read_cap_id=own_rcap.id,
        conversation=convo,
    )
    convo.own_peer = own_peer
    sess.add(wcap)
    sess.add(own_rcap)
    sess.add(convo)
    sess.add(own_peer)

    peers: "dict[str, persistent.ConversationPeer]" = {}
    for pname, cap in peer_caps.items():
        rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=cap)
        peer = persistent.ConversationPeer(
            name=pname,
            read_cap_id=rcap.id,
            conversation=convo,
        )
        sess.add(rcap)
        sess.add(peer)
        peers[pname] = peer

    await sess.flush()
    return convo, peers


def _blob(
    survey_id: bytes,
    *,
    slots: "list[str] | None" = None,
    creator: "bytes | None" = None,
) -> bytes:
    blob: bytes = sync.full_state(
        schema.new_survey_doc(
            survey_id,
            "t",
            Mode.APPROVAL,
            slots or ["a", "b"],
            creator=creator,
        )
    )
    return blob


def _raw(
    kind: GroupChatTypeEnum, tally: "GroupChatTally | None"
) -> GroupChatMessage:
    return GroupChatMessage(
        version=0,
        msg_type=kind,
        tally=tally,
    )


@pytest.mark.asyncio
async def test_voter_id_falls_back_to_the_local_id_without_a_read_cap() -> (
    None
):
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", None, {})
        doc = await ctrl.create_local(
            sess,
            convo,
            survey_id,
            "t",
            Mode.APPROVAL,
            ["a"],
        )
        expected = voter_id_from_read_cap(convo.own_peer.read_cap_id.bytes)
        await sess.commit()

    assert schema.creator_of(doc) == expected


@pytest.mark.asyncio
async def test_surveys_lists_every_loaded_key() -> None:
    ctrl = TallyController()
    first, second = uuid.uuid4().bytes, uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        await ctrl.create_local(sess, convo, first, "a", Mode.APPROVAL, ["a"])
        await ctrl.create_local(
            sess, convo, second, "b", Mode.APPROVAL, ["a"]
        )
        convo_id = convo.id
        await sess.commit()

    assert sorted(ctrl.surveys()) == sorted(
        [(convo_id, first), (convo_id, second)]
    )


@pytest.mark.asyncio
async def test_close_local_loads_a_survey_that_is_only_persisted() -> None:
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=convo.id,
                doc_state=_blob(survey_id),
            )
        )
        await sess.commit()

    ctrl = TallyController()
    async with persistent.asession() as sess:
        convo = (await sess.exec(select(persistent.Conversation))).one()
        assert await ctrl.close_local(sess, convo, survey_id) is True
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).status == "closed"


@pytest.mark.asyncio
async def test_ensure_loaded_refuses_another_conversations_survey() -> None:
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo_a, _pa = await _make_convo(sess, "a", OWN_CAP, {})
        convo_b, _pb = await _make_convo(sess, "b", BOB_CAP, {})
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=convo_a.id,
                doc_state=_blob(survey_id),
            )
        )
        b_id = convo_b.id
        await sess.commit()

    ctrl = TallyController()
    async with persistent.asession() as sess:
        convo_b = (
            await sess.exec(
                select(persistent.Conversation).where(
                    persistent.Conversation.id == b_id,
                )
            )
        ).one()
        assert await ctrl.close_local(sess, convo_b, survey_id) is False
        await sess.commit()

    assert ctrl.get(b_id, survey_id) is None


@pytest.mark.asyncio
async def test_save_refuses_to_overwrite_a_foreign_conversations_survey(
    caplog: pytest.LogCaptureFixture,
) -> None:
    survey_id = uuid.uuid4().bytes
    original = _blob(survey_id)
    async with persistent.asession() as sess:
        convo_a, _pa = await _make_convo(sess, "a", OWN_CAP, {})
        convo_b, _pb = await _make_convo(sess, "b", BOB_CAP, {})
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=convo_a.id,
                doc_state=original,
            )
        )
        a_id, b_id = convo_a.id, convo_b.id
        await sess.commit()

    ctrl = TallyController()
    with caplog.at_level(logging.WARNING):
        async with persistent.asession() as sess:
            convo_b = (
                await sess.exec(
                    select(persistent.Conversation).where(
                        persistent.Conversation.id == b_id,
                    )
                )
            ).one()
            await ctrl.create_local(
                sess,
                convo_b,
                survey_id,
                "hijack",
                Mode.APPROVAL,
                ["a"],
            )
            await sess.commit()

    assert "refusing to overwrite" in caplog.text
    async with persistent.asession() as sess:
        row = await sess.get(persistent.TallyState, survey_id)
        assert row is not None
        assert row.conversation_id == a_id
        assert row.doc_state == original


@pytest.mark.asyncio
async def test_close_local_refuses_an_unknown_survey() -> None:
    ctrl = TallyController()
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        assert (
            await ctrl.close_local(sess, convo, uuid.uuid4().bytes) is False
        )
        await sess.commit()


@pytest.mark.asyncio
async def test_cast_local_vote_returns_none_for_an_unknown_survey() -> None:
    ctrl = TallyController()
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        got = await ctrl.cast_local_vote(
            sess,
            convo,
            uuid.uuid4().bytes,
            {"s0": "yes"},
        )
        assert got is None
        await sess.commit()


@pytest.mark.asyncio
async def test_list_for_conversation_returns_cached_and_persisted_docs() -> (
    None
):
    cached, stored = uuid.uuid4().bytes, uuid.uuid4().bytes
    ctrl = TallyController()
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        await ctrl.create_local(
            sess, convo, cached, "a", Mode.APPROVAL, ["a"]
        )
        sess.add(
            persistent.TallyState(
                survey_id=stored,
                conversation_id=convo.id,
                doc_state=_blob(stored),
            )
        )
        convo_id = convo.id
        await sess.commit()

    async with persistent.asession() as sess:
        docs = await ctrl.list_for_conversation(sess, convo_id)

    assert sorted(schema.survey_id_of(d) for d in docs) == sorted(
        [cached, stored]
    )
    assert ctrl.get(convo_id, stored) is not None


@pytest.mark.asyncio
async def test_create_without_a_crdt_payload_stores_nothing() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        message = _raw(
            GroupChatTypeEnum.TALLY_CREATE,
            GroupChatTally(survey_id=survey_id),
        )
        assert (
            await ctrl.handle_event(sess, peers["alice"], message)
        ).status == "applied"
        convo_id = convo.id
        await sess.commit()

    assert ctrl.get(convo_id, survey_id) is None
    async with persistent.asession() as sess:
        assert await sess.get(persistent.TallyState, survey_id) is None


@pytest.mark.asyncio
async def test_sync_response_merges_a_diff_into_an_existing_doc() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        local = await ctrl.create_local(
            sess,
            convo,
            survey_id,
            "t",
            Mode.APPROVAL,
            ["a", "b"],
        )
        remote = sync.load_doc(sync.full_state(local))
        engine.apply_vote(
            remote, voter_id_from_read_cap(ALICE_CAP), {"s0": "yes"}
        )
        diff = sync.diff_since(remote, sync.state_vector(local))

        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_sync_response(survey_id, diff),
        )
        assert result.status == "applied"
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).n_voters == 1


@pytest.mark.asyncio
async def test_sync_response_with_undecodable_crdt_leaves_the_doc_alone() -> (
    None
):
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        await ctrl.create_local(
            sess, convo, survey_id, "t", Mode.APPROVAL, ["a"]
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_sync_response(survey_id, b"\xde\xad\xbe\xef" * 8),
        )
        assert result.status == "applied"
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).n_voters == 0


@pytest.mark.asyncio
async def test_close_before_create_is_replayed_for_the_creator() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess,
            "g",
            OWN_CAP,
            {"alice": ALICE_CAP, "bob": BOB_CAP},
        )
        early = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_close(survey_id),
        )
        assert early.status == "duplicate"
        await ctrl.handle_event(
            sess,
            peers["bob"],
            events.build_create(
                survey_id,
                _blob(survey_id, creator=voter_id_from_read_cap(ALICE_CAP)),
            ),
        )
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).status == "closed"


@pytest.mark.asyncio
async def test_close_before_create_from_a_non_creator_is_discarded() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess,
            "g",
            OWN_CAP,
            {"alice": ALICE_CAP, "bob": BOB_CAP},
        )
        await ctrl.handle_event(
            sess, peers["alice"], events.build_close(survey_id)
        )
        await ctrl.handle_event(
            sess,
            peers["bob"],
            events.build_create(
                survey_id,
                _blob(survey_id, creator=voter_id_from_read_cap(BOB_CAP)),
            ),
        )
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).status == "open"


@pytest.mark.asyncio
async def test_close_before_create_is_a_noop_when_closed() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        await ctrl.handle_event(
            sess, peers["alice"], events.build_close(survey_id)
        )
        closed = schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a"])
        engine.close_survey(closed)
        await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_create(survey_id, sync.full_state(closed)),
        )
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).status == "closed"


@pytest.mark.asyncio
async def test_a_buffered_older_vote_does_not_supersede_the_stored_one() -> (
    None
):
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    carol_id = voter_id_from_read_cap(CAROL_CAP)
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess,
            "g",
            OWN_CAP,
            {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        await ctrl.handle_event(
            sess,
            peers["carol"],
            events.build_vote(survey_id, {"s0": "yes"}, 0),
        )
        newer = schema.new_survey_doc(
            survey_id, "t", Mode.APPROVAL, ["a", "b"]
        )
        engine.apply_vote(newer, carol_id, {"s1": "no"}, 5)
        await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_create(survey_id, sync.full_state(newer)),
        )
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.stored_choice(doc, carol_id) == (5, {"s1": "no"})


@pytest.mark.asyncio
async def test_a_buffered_invalid_ballot_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess,
            "g",
            OWN_CAP,
            {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        await ctrl.handle_event(
            sess,
            peers["carol"],
            events.build_vote(survey_id, {"s9": "yes"}),
        )
        with caplog.at_level(logging.WARNING):
            await ctrl.handle_event(
                sess,
                peers["alice"],
                events.build_create(survey_id, _blob(survey_id, slots=["a"])),
            )
        convo_id = convo.id
        await sess.commit()

    assert "invalid ballot" in caplog.text
    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).n_voters == 0


@pytest.mark.asyncio
async def test_reconcile_skips_rows_it_cannot_use() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    text = GroupChatMessage(version=0, text="hi")
    payloads = [
        b"C" + events.build_vote(survey_id, {"s0": "yes"}).to_cbor(),
        b"F" + b"not cbor at all",
        b"F" + text.to_cbor(),
    ]
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"carol": CAROL_CAP}
        )
        for order, payload in enumerate(payloads):
            sess.add(
                persistent.ConversationLog(
                    conversation_id=convo.id,
                    conversation_peer_id=peers["carol"].id,
                    conversation_order=order,
                    payload=payload,
                )
            )
        sess.add(
            persistent.ConversationLog(
                conversation_id=convo.id,
                conversation_peer_id=999999,
                conversation_order=len(payloads),
                payload=b"F"
                + events.build_vote(survey_id, {"s0": "yes"}).to_cbor(),
            )
        )
        await sess.commit()

    await ctrl.reconcile_from_log()
    assert ctrl._pending == {}


@pytest.mark.asyncio
async def test_a_tally_message_without_a_payload_is_rejected() -> None:
    ctrl = TallyController()
    async with persistent.asession() as sess:
        _convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            _raw(GroupChatTypeEnum.TALLY_VOTE, None),
        )
        assert result.status == "rejected"
        assert result.detail == "tally message with no payload"
        await sess.commit()


@pytest.mark.asyncio
async def test_an_invalid_vote_is_rejected_with_a_reason() -> None:
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        await ctrl.create_local(
            sess, convo, survey_id, "t", Mode.APPROVAL, ["a"]
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_vote(survey_id, {"s9": "yes"}),
        )
        assert result.status == "rejected"
        assert "invalid vote" in result.detail
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
    assert doc is not None
    assert engine.tally(doc).n_voters == 0


@pytest.mark.asyncio
async def test_a_sync_request_for_an_unknown_survey_is_a_noop() -> None:
    ctrl = TallyController()
    async with persistent.asession() as sess:
        _convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_sync_request(
                uuid.uuid4().bytes, sync.state_vector(Doc())
            ),
        )
        assert result.status == "duplicate"
        assert result.signal_send is False
        await sess.commit()


@pytest.mark.asyncio
async def test_a_sync_request_stages_a_response_on_the_outgoing_stream() -> (
    None
):
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        await ctrl.create_local(
            sess, convo, survey_id, "t", Mode.APPROVAL, ["a"]
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            events.build_sync_request(survey_id, sync.state_vector(Doc())),
        )
        assert result.status == "applied"
        assert result.signal_send is True
        convo_id = convo.id
        await sess.commit()

    async with persistent.asession() as sess:
        staged = (
            await sess.exec(
                select(persistent.PlaintextWAL).where(
                    persistent.PlaintextWAL.conversation_id == convo_id,
                )
            )
        ).all()
    assert staged


@pytest.mark.asyncio
async def test_an_unhandled_tally_kind_is_rejected() -> None:
    ctrl = TallyController()
    async with persistent.asession() as sess:
        _convo, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP}
        )
        result = await ctrl.handle_event(
            sess,
            peers["alice"],
            _raw(
                GroupChatTypeEnum.TEXT,
                GroupChatTally(survey_id=uuid.uuid4().bytes),
            ),
        )
        assert result.status == "rejected"
        assert "unhandled tally kind" in result.detail
        await sess.commit()


@pytest.mark.asyncio
async def test_staging_a_chunked_message_registers_its_cap() -> None:
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _peers = await _make_convo(sess, "g", OWN_CAP, {})
        before = len((await sess.exec(select(persistent.WriteCapWAL))).all())
        await send.stage_outbound(
            sess,
            convo,
            events.build_create(survey_id, b"\x00" * 4096),
        )
        await sess.commit()

    async with persistent.asession() as sess:
        after = len((await sess.exec(select(persistent.WriteCapWAL))).all())
    assert after == before + 1


@pytest.mark.asyncio
async def test_a_sync_response_for_a_foreign_conversation_is_rejected() -> (
    None
):
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo_a, _pa = await _make_convo(sess, "a", OWN_CAP, {})
        convo_b, peers_b = await _make_convo(
            sess, "b", BOB_CAP, {"bob": BOB_CAP}
        )
        sess.add(
            persistent.TallyState(
                survey_id=survey_id,
                conversation_id=convo_a.id,
                doc_state=_blob(survey_id),
            )
        )
        b_id = convo_b.id
        await sess.flush()

        result = await ctrl.handle_event(
            sess,
            peers_b["bob"],
            events.build_sync_response(survey_id, _blob(survey_id)),
        )
        assert result.status == "rejected"
        assert result.detail == "survey belongs to another conversation"
        await sess.commit()

    assert ctrl.get(b_id, survey_id) is None
