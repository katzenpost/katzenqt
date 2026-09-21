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
CAROL_CAP = bytes([0x05]) * 136
OWN_CAP = bytes([0x01]) * 136


def test_voter_id_ignores_the_read_cap_index_suffix():
    """Identity is the 32-byte public key, not the 104-byte index suffix: a
    joiner's pre-mutation cap and the salt-mutated cap the group holds for the
    same member must hash to the same voter id (and different members must
    not collide)."""
    key = bytes([0x0A]) * 32
    own_copy = key + bytes([0x01]) * 104
    shared_copy = key + bytes([0x02]) * 104
    other = bytes([0x0B]) * 32 + bytes([0x03]) * 104

    assert voter_id_from_read_cap(own_copy) == voter_id_from_read_cap(shared_copy)
    assert voter_id_from_read_cap(other) != voter_id_from_read_cap(shared_copy)


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
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
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
        convo_id = convo.id
        await sess.commit()

    doc = ctrl.get(convo_id, survey_id)
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
        convo_id = convo.id
        await sess.commit()

    reloaded = TallyController()
    await reloaded.load_all()
    res = engine.tally(reloaded.get(convo_id, survey_id))
    assert res.n_voters == 1
    assert res.slots[0].maybe == 1


@pytest.mark.asyncio
async def test_dispatch_logs_tally_rows_and_routes_chat_too():
    survey_id = uuid.uuid4().bytes
    conversation_handlers.tally_controller.INSTANCE._docs.clear()
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})

        blob = sync.full_state(schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a"]))
        create = events.build_create(survey_id, blob)
        added, _sig, _pa, tally_added = await conversation_handlers.dispatch(
            sess, peers["alice"], create, b"F" + create.to_cbor(),
        )
        assert added is True  # a tally message is an ordinary chat row
        assert tally_added is True  # ...and the poll views must refresh

        vote = events.build_vote(survey_id, {"s0": "yes"})
        added, _sig, _pa, tally_added = await conversation_handlers.dispatch(
            sess, peers["alice"], vote, b"F" + vote.to_cbor(),
        )
        assert added is True
        assert tally_added is True

        text = models.GroupChatMessage(version=0, membership_hash=bytes(32), text="hi")
        added, _sig, _pa, tally_added = await conversation_handlers.dispatch(
            sess, peers["alice"], text, b"F" + text.to_cbor(),
        )
        assert added is True  # ordinary chat still lands in the log
        assert tally_added is False
        convo_id = convo.id  # capture before commit expires the attribute
        await sess.commit()

        rows = (await sess.exec(
            select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id
            )
        )).all()
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_creator_can_close_but_non_creator_cannot_locally():
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})

        # We created it, so we may close it.
        await ctrl.create_local(sess, convo, survey_id, "x", Mode.APPROVAL, ["a"])
        assert await ctrl.close_local(sess, convo, survey_id) is True
        assert engine.tally(ctrl.get(convo.id, survey_id)).status == "closed"

        # A survey whose creator is Alice: our local user must not close it.
        other = uuid.uuid4().bytes
        blob = sync.full_state(schema.new_survey_doc(
            other, "y", Mode.APPROVAL, ["a"], creator=voter_id_from_read_cap(ALICE_CAP)))
        await ctrl.handle_event(sess, peers["alice"], events.build_create(other, blob))
        assert await ctrl.close_local(sess, convo, other) is False
        assert engine.tally(ctrl.get(convo.id, other)).status == "open"


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
        assert engine.tally(ctrl.get(convo.id, survey_id)).status == "open"

        # A close from the creator's read cap is honoured.
        await ctrl.handle_event(sess, peers["creator"], events.build_close(survey_id))
        assert engine.tally(ctrl.get(convo.id, survey_id)).status == "closed"
        await sess.commit()


@pytest.mark.asyncio
async def test_cross_conversation_apply_is_isolated():
    """A survey created in conversation A cannot be seized or perturbed by a
    peer in conversation B who reuses its survey id."""
    from katzenqt.tally import controller as controller_mod

    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo_a, _own_a, peers_a = await _make_convo(sess, "a", OWN_CAP, {"alice": ALICE_CAP})
        convo_b, _own_b, peers_b = await _make_convo(sess, "b", bytes([0x04]) * 136, {"bob": BOB_CAP})

        blob = sync.full_state(schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a"]))
        await ctrl.handle_event(sess, peers_a["alice"], events.build_create(survey_id, blob))
        await ctrl.handle_event(sess, peers_a["alice"], events.build_vote(survey_id, {"s0": "yes"}))

        other_blob = sync.full_state(schema.new_survey_doc(survey_id, "hijack", Mode.APPROVAL, ["a"]))
        assert (await ctrl.handle_event(sess, peers_b["bob"], events.build_create(survey_id, other_blob))).status == "rejected"
        assert (await ctrl.handle_event(sess, peers_b["bob"], events.build_vote(survey_id, {"s0": "no"}))).status != "applied"

        a_id, b_id = convo_a.id, convo_b.id
        await sess.commit()

    assert ctrl.get(b_id, survey_id) is None
    a_doc = ctrl.get(a_id, survey_id)
    assert engine.tally(a_doc).slots[0].yes == 1

    async with persistent.asession() as sess:
        row = await sess.get(persistent.TallyState, survey_id)
        assert row.conversation_id == a_id


@pytest.mark.asyncio
async def test_oversized_survey_id_is_dropped():
    ctrl = TallyController()
    huge_id = b"\x01" * (controller_max_survey_id() + 1)
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})
        blob = sync.full_state(schema.new_survey_doc(huge_id, "t", Mode.APPROVAL, ["a"]))
        assert (await ctrl.handle_event(sess, peers["alice"], events.build_create(huge_id, blob))).status == "rejected"
        convo_id = convo.id
        await sess.commit()

    assert ctrl.get(convo_id, huge_id) is None
    async with persistent.asession() as sess:
        assert await sess.get(persistent.TallyState, huge_id) is None


@pytest.mark.asyncio
async def test_oversized_crdt_blob_is_dropped():
    from katzenqt.tally import controller as controller_mod

    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})
        giant = b"\x00" * (controller_mod._MAX_CRDT_BLOB + 1)
        assert (await ctrl.handle_event(sess, peers["alice"], events.build_create(survey_id, giant))).status == "rejected"
        convo_id = convo.id
        await sess.commit()

    assert ctrl.get(convo_id, survey_id) is None
    async with persistent.asession() as sess:
        assert await sess.get(persistent.TallyState, survey_id) is None


def controller_max_survey_id() -> int:
    from katzenqt.tally import controller as controller_mod

    return controller_mod._MAX_SURVEY_ID_LEN


@pytest.mark.asyncio
async def test_undecodable_crdt_is_dropped_not_raised(caplog):
    from katzenqt.tally import sync
    with pytest.raises(ValueError):
        sync.load_doc(b"\xde\xad\xbe\xef" * 8)


@pytest.mark.asyncio
async def test_malformed_sync_request_is_dropped_not_raised(caplog):
    """A TALLY_SYNC_REQ carrying a garbage state vector must not raise out of
    dispatch (which would wedge the receive loop); it is dropped like any other
    undecodable tally payload, and no reply is staged (False return)."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})
        # A sender with no useful history sends a malformed state vector.
        await ctrl.create_local(sess, convo, survey_id, "t", Mode.APPROVAL, ["a"])
        bad_req = events.build_sync_request(survey_id, b"\xde\xad\xbe\xef" * 16)
        assert (await ctrl.handle_event(sess, peers["alice"], bad_req)).status == "rejected"
        await sess.commit()


@pytest.mark.asyncio
async def test_apply_and_reject_verdicts_for_received_events():
    """`handle_event` reports applied/rejected for the timeline, and a rejected
    event changes nothing."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(sess, "g", OWN_CAP, {"alice": ALICE_CAP})
        blob = sync.full_state(schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a"]))
        create = await ctrl.handle_event(sess, peers["alice"], events.build_create(survey_id, blob))
        assert create.status == "applied"
        convo_id = convo.id
        await sess.commit()

    from katzenqt.conversation_handlers import _conversation_peers
    async with persistent.asession() as sess:
        alice = next(
            p for p in await _conversation_peers(sess, convo_id) if p.name == "alice"
        )
        applied = await ctrl.handle_event(sess, alice, events.build_vote(survey_id, {"s0": "yes"}))
        assert applied.status == "applied"
        # An oversized survey id is rejected and changes nothing.
        rejected = await ctrl.handle_event(
            sess, alice, events.build_create(b"\x01" * 65, blob),
        )
        assert rejected.status == "rejected"
        assert rejected.detail
        await sess.commit()

    assert engine.tally(ctrl.get(convo_id, survey_id)).n_voters == 1


# ---------------------------------------------------------------------------
# Out-of-order votes: a vote consumed before the survey it names
# ---------------------------------------------------------------------------


def _created_doc_blob(survey_id, topic="t", slots=("a", "b")):
    doc = schema.new_survey_doc(survey_id, topic, Mode.APPROVAL, list(slots))
    return sync.full_state(doc)


@pytest.mark.asyncio
async def test_vote_before_create_is_applied_when_the_survey_arrives():
    """A vote on one member's stream can be consumed before the create on
    another's. It is a no-op while the survey is unknown, then applied (from
    the buffer) the moment the create builds the Doc."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        vote = await ctrl.handle_event(
            sess, peers["carol"], events.build_vote(survey_id, {"s0": "yes"}),
        )
        assert vote.status == "duplicate"  # unknown survey: buffered, not applied
        applied = await ctrl.handle_event(
            sess, peers["alice"],
            events.build_create(survey_id, _created_doc_blob(survey_id)),
        )
        assert applied.status == "applied"
        convo_id = convo.id
        await sess.commit()

    result = engine.tally(ctrl.get(convo_id, survey_id))
    assert result.n_voters == 1
    assert result.slots[0].yes == 1


@pytest.mark.asyncio
async def test_reconcile_buffers_an_early_vote_then_the_create_applies_it():
    """A vote whose create was never processed in a previous session is
    recovered from the ConversationLog at startup and applied when the create
    finally arrives."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        vote = events.build_vote(survey_id, {"s0": "yes"})
        sess.add(persistent.ConversationLog(
            conversation_id=convo.id, conversation_peer_id=peers["carol"].id,
            conversation_order=0, payload=b"F" + vote.to_cbor(),
        ))
        convo_id = convo.id
        await sess.commit()

    await ctrl.reconcile_from_log()
    assert (convo_id, survey_id) in ctrl._pending

    from katzenqt.conversation_handlers import _conversation_peers

    async with persistent.asession() as sess:
        alice = next(
            p for p in await _conversation_peers(sess, convo_id) if p.name == "alice"
        )
        applied = await ctrl.handle_event(
            sess, alice,
            events.build_create(survey_id, _created_doc_blob(survey_id)),
        )
        assert applied.status == "applied"
        await sess.commit()

    result = engine.tally(ctrl.get(convo_id, survey_id))
    assert result.n_voters == 1
    assert result.slots[0].yes == 1
    assert (convo_id, survey_id) not in ctrl._pending


@pytest.mark.asyncio
async def test_reconcile_ignores_surveys_we_already_persisted():
    """Once a survey is persisted its ballots are already in the Doc, so the
    log scan must not re-buffer them (that is what makes startup replay
    idempotent)."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        doc = schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a", "b"])
        sess.add(persistent.TallyState(
            survey_id=survey_id, conversation_id=convo.id,
            doc_state=sync.full_state(doc),
        ))
        vote = events.build_vote(survey_id, {"s0": "yes"})
        sess.add(persistent.ConversationLog(
            conversation_id=convo.id, conversation_peer_id=peers["carol"].id,
            conversation_order=0, payload=b"F" + vote.to_cbor(),
        ))
        convo_id = convo.id
        await sess.commit()

    await ctrl.reconcile_from_log()
    assert ctrl._pending == {}


@pytest.mark.asyncio
async def test_buffered_vote_already_in_the_create_blob_is_not_double_applied():
    """A create may already carry a ballot (e.g. the creator had it); the
    buffered copy must not overwrite or duplicate it."""
    ctrl = TallyController()
    survey_id = uuid.uuid4().bytes
    async with persistent.asession() as sess:
        convo, _own, peers = await _make_convo(
            sess, "g", OWN_CAP, {"alice": ALICE_CAP, "carol": CAROL_CAP},
        )
        await ctrl.handle_event(
            sess, peers["carol"], events.build_vote(survey_id, {"s0": "yes"}),
        )
        doc = schema.new_survey_doc(survey_id, "t", Mode.APPROVAL, ["a", "b"])
        engine.apply_vote(doc, voter_id_from_read_cap(CAROL_CAP), {"s0": "yes"}, 0)
        await ctrl.handle_event(
            sess, peers["alice"],
            events.build_create(survey_id, sync.full_state(doc)),
        )
        convo_id = convo.id
        await sess.commit()

    result = engine.tally(ctrl.get(convo_id, survey_id))
    assert result.n_voters == 1
    assert result.slots[0].yes == 1
