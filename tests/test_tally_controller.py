"""Controller behaviour against a real (in-memory) database, no network.

Covers the security property (a vote is keyed to the authenticated sender, not
to anything in the payload), the persistence round-trip (state survives a fresh
controller), and the dispatch routing (tally messages bypass the chat log while
ordinary messages still land in it).
"""
from __future__ import annotations

import uuid

import pytest
from sqlmodel import select

from katzenqt import conversation_handlers, models, persistent
from katzenqt.tally import engine, events, schema, sync
from katzenqt.tally.controller import TallyController, voter_id_from_read_cap
from katzenqt.tally.schema import Mode, votes_map

ALICE_CAP = bytes([0x02]) * 136
BOB_CAP = bytes([0x03]) * 136
OWN_CAP = bytes([0x01]) * 136


async def _make_convo(sess, name, own_cap, peer_caps):
    """Build a conversation with an own peer and named remote peers, each with
    a provisioned read capability. Mirrors headless ``create-conv``."""
    wcap = persistent.WriteCapWAL(id=uuid.uuid4())
    own_rcap = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=wcap.id, read_cap=own_cap)
    convo = persistent.Conversation(name=name, write_cap=wcap.id)
    own_peer = persistent.ConversationPeer(name="me", read_cap_id=own_rcap.id, conversation=convo)
    convo.own_peer = own_peer
    sess.add(wcap)
    sess.add(own_rcap)
    sess.add(convo)
    sess.add(own_peer)

    peers = {}
    for pname, cap in peer_caps.items():
        rcap = persistent.ReadCapWAL(id=uuid.uuid4(), read_cap=cap)
        peer = persistent.ConversationPeer(name=pname, read_cap_id=rcap.id, conversation=convo)
        sess.add(rcap)
        sess.add(peer)
        peers[pname] = peer

    # Flush, not commit: assigns primary keys while keeping the objects live
    # and unexpired, so the caller may go on using them in the same async
    # session without tripping a synchronous lazy-load.
    await sess.flush()
    return convo, own_peer, peers


@pytest.mark.asyncio
async def test_vote_is_keyed_to_the_authenticated_sender():
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "bob": BOB_CAP},
        )
        await ctrl.create_local(sess, convo, survey_id, "lunch?", Mode.APPROVAL, ["a", "b"])
        await ctrl.handle_event(sess, peers["alice"], events.build_vote(survey_id, {"s0": "yes"}))
        await ctrl.handle_event(sess, peers["bob"], events.build_vote(survey_id, {"s1": "yes"}))
        await sess.commit()

    doc = ctrl.get(survey_id)
    keys = set(votes_map(doc).keys())
    assert keys == {
        voter_id_from_read_cap(ALICE_CAP).hex(),
        voter_id_from_read_cap(BOB_CAP).hex(),
    }
    res = engine.tally(doc)
    assert res.n_voters == 2
    by = {s.slot_id: s for s in res.slots}
    assert by["s0"].yes == 1 and by["s1"].yes == 1


@pytest.mark.asyncio
async def test_one_peer_cannot_overwrite_anothers_vote():
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "bob": BOB_CAP},
        )
        await ctrl.create_local(sess, convo, survey_id, "x", Mode.APPROVAL, ["a"])
        await ctrl.handle_event(sess, peers["bob"], events.build_vote(survey_id, {"s0": "yes"}))
        # Alice votes the opposite. The payload carries no voter id, so the
        # controller can only ever write Alice's own key; Bob's stands.
        await ctrl.handle_event(sess, peers["alice"], events.build_vote(survey_id, {"s0": "no"}))
        await sess.commit()

    doc = ctrl.get(survey_id)
    vmap = votes_map(doc)
    bob_key = voter_id_from_read_cap(BOB_CAP).hex()
    assert vmap[bob_key]["s0"] == "yes"
    assert engine.tally(doc).slots[0].yes == 1  # only Bob's yes


@pytest.mark.asyncio
async def test_state_persists_and_a_fresh_controller_reloads_it():
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})
        ctrl = TallyController()
        await ctrl.create_local(sess, convo, survey_id, "t", Mode.AVAILABILITY, ["a", "b"])
        await ctrl.handle_event(sess, peers["alice"], events.build_vote(survey_id, {"s0": "maybe"}))
        await sess.commit()

    reloaded = TallyController()
    await reloaded.load_all()
    res = engine.tally(reloaded.get(survey_id))
    assert res.n_voters == 1
    assert res.slots[0].maybe == 1


@pytest.mark.asyncio
async def test_dispatch_routes_tally_off_the_log_and_chat_into_it():
    survey_id = uuid.uuid4().bytes
    conversation_handlers.tally_controller.INSTANCE._docs.clear()
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})

        blob = sync.full_state(schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a"]))
        create = events.build_create(survey_id, blob)
        added, _sig = await conversation_handlers.dispatch(sess, peers["alice"], create, b"ignored")
        assert added is False  # tally create does not add a chat row

        vote = events.build_vote(survey_id, {"s0": "yes"})
        added, _sig = await conversation_handlers.dispatch(sess, peers["alice"], vote, b"ignored")
        assert added is False

        text = models.GroupChatMessage(version=0, membership_hash=bytes(32), text="hi")
        added, _sig = await conversation_handlers.dispatch(
            sess, peers["alice"], text, b"F" + text.to_cbor(),
        )
        assert added is True  # ordinary chat still lands in the log
        convo_id = convo.id  # capture before commit expires the attribute
        await sess.commit()

        rows = (await sess.exec(
            select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id
            )
        )).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_creator_can_close_but_non_creator_cannot_locally():
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})

        # We created it, so we may close it.
        await ctrl.create_local(sess, convo, survey_id, "x", Mode.APPROVAL, ["a"])
        assert await ctrl.close_local(sess, convo, survey_id) is True
        assert engine.tally(ctrl.get(survey_id)).status == "closed"

        # A survey whose creator is Alice: our local user must not close it.
        other = uuid.uuid4().bytes
        blob = sync.full_state(schema.new_survey_doc(
            other, "y", Mode.APPROVAL, ["a"], creator=voter_id_from_read_cap(ALICE_CAP)))
        await ctrl.handle_event(sess, peers["alice"], events.build_create(other, blob))
        assert await ctrl.close_local(sess, convo, other) is False
        assert engine.tally(ctrl.get(other)).status == "open"


@pytest.mark.asyncio
async def test_close_event_honoured_only_from_the_creator():
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        # The creator (OWN_CAP) is also reachable as a remote peer "creator".
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "creator": OWN_CAP})
        await ctrl.create_local(sess, convo, survey_id, "x", Mode.APPROVAL, ["a"])

        # A close from Alice (not the creator) is ignored.
        await ctrl.handle_event(sess, peers["alice"], events.build_close(survey_id))
        assert engine.tally(ctrl.get(survey_id)).status == "open"

        # A close from the creator's read cap is honoured.
        await ctrl.handle_event(sess, peers["creator"], events.build_close(survey_id))
        assert engine.tally(ctrl.get(survey_id)).status == "closed"
        await sess.commit()
