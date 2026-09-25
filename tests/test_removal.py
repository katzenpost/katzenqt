import asyncio
import uuid

import cbor2
import pytest
from sqlmodel import select

from katzenqt import network, persistent, removal
from katzenqt.tally import controller as tally_controller
from tests.test_membership_hash import _make_conversation


def _mixwal(stream: uuid.UUID, *, is_read: bool, plaintextwal=None) -> persistent.MixWAL:
    return persistent.MixWAL(
        bacap_stream=stream, plaintextwal=plaintextwal,
        envelope_hash=uuid.uuid4().bytes, encrypted_payload=b"x",
        envelope_descriptor=b"x", current_message_index=b"\x00" * 104,
        next_message_index=b"\x00" * 104, is_read=is_read,
    )


def _marker(rel_path: str) -> bytes:
    return b"F" + cbor2.dumps({"kind": "file_marker", "rel_path": rel_path})


def _spill(rel_path: str) -> None:
    path = persistent.state_file.parent / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")


async def _count(model) -> int:
    async with persistent.asession() as sess:
        return len((await sess.exec(select(model))).all())


async def _alice(conv_id: int) -> persistent.ConversationPeer:
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        return next(p for p in conv.peers if p.name == "alice")


async def _log(conv_id: int, peer_id: int, order: int, payload: bytes = b"hi") -> None:
    async with persistent.asession() as sess:
        sess.add(persistent.ConversationLog(
            conversation_id=conv_id, conversation_peer_id=peer_id,
            conversation_order=order, payload=payload,
        ))
        await sess.commit()


async def _add_substream(conv_id: int, parent: persistent.ConversationPeer) -> uuid.UUID:
    rcw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        sess.add(persistent.ReadCapWAL(id=rcw_id, read_cap=b"\x05" * 136))
        sess.add(persistent.ConversationPeer(
            name=f":substream:{parent.id}:ab12", read_cap_id=rcw_id, conversation=conv,
        ))
        sess.add(persistent.ReceivedPiece(
            read_cap=rcw_id, bacap_index=b"\x00" * 8, chunk_type=b"C", chunk=b"c",
        ))
        await sess.commit()
    return rcw_id


@pytest.mark.asyncio
async def test_remove_peer_drops_everything_about_them():
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    sub = await _add_substream(conv_id, alice)
    async with persistent.asession() as sess:
        sess.add(_mixwal(alice.read_cap_id, is_read=True))
        await sess.commit()
    await _log(conv_id, alice.id, 1)

    await removal.remove_peer(conversation_id=conv_id, peer_id=alice.id)

    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        assert [p.name for p in conv.peers] == ["me"]
        for stream in (alice.read_cap_id, sub):
            assert await sess.get(persistent.ReadCapWAL, stream) is None
    for model in (persistent.MixWAL, persistent.ReceivedPiece, persistent.ConversationLog):
        assert await _count(model) == 0
    assert await _count(persistent.ConversationPeerLink) == 1


@pytest.mark.asyncio
async def test_remove_peer_keeps_own_messages_and_the_conversation():
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        own = conv.own_peer_id
    await _log(conv_id, own, 1, b"mine")
    await _log(conv_id, alice.id, 2, b"hers")

    await removal.remove_peer(conversation_id=conv_id, peer_id=alice.id)

    async with persistent.asession() as sess:
        logs = (await sess.exec(select(persistent.ConversationLog))).all()
        assert [row.payload for row in logs] == [b"mine"]
        assert await sess.get(persistent.Conversation, conv_id) is not None


@pytest.mark.asyncio
async def test_remove_peer_refuses_self_and_strangers():
    conv_id = await _make_conversation()
    other = await _make_conversation("other")
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        own = conv.own_peer_id
    with pytest.raises(removal.RemovalError):
        await removal.remove_peer(conversation_id=conv_id, peer_id=own)
    stranger = await _alice(other)
    with pytest.raises(removal.RemovalError):
        await removal.remove_peer(conversation_id=conv_id, peer_id=stranger.id)
    assert await _count(persistent.ConversationPeer) == 4


@pytest.mark.asyncio
async def test_remove_peer_deletes_only_unshared_attachments():
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        own = conv.own_peer_id
    unique = f"attachments/{conv_id}/unique.bin"
    shared = f"attachments/{conv_id}/shared.bin"
    for rel in (unique, shared):
        _spill(rel)
    await _log(conv_id, alice.id, 1, _marker(unique))
    await _log(conv_id, alice.id, 2, _marker(shared))
    await _log(conv_id, own, 3, _marker(shared))

    await removal.remove_peer(conversation_id=conv_id, peer_id=alice.id)

    assert not (persistent.state_file.parent / unique).exists()
    assert (persistent.state_file.parent / shared).exists()


@pytest.mark.asyncio
async def test_removal_never_deletes_outside_the_attachments_dir(tmp_path):
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    victim = persistent.state_file.parent / "victim.txt"
    victim.write_text("keep")
    await _log(conv_id, alice.id, 1, _marker("attachments/../victim.txt"))

    await removal.remove_peer(conversation_id=conv_id, peer_id=alice.id)

    assert victim.exists()


@pytest.mark.asyncio
async def test_remove_peer_cancels_its_reader_first():
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    entered = asyncio.Event()

    async def reader() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(reader())
    network._inflight_reads[alice.read_cap_id] = task
    await asyncio.wait_for(entered.wait(), timeout=2)

    await removal.remove_peer(conversation_id=conv_id, peer_id=alice.id)

    assert task.cancelled()
    assert alice.read_cap_id not in network._inflight_reads


async def _populate_conversation(conv_id: int) -> uuid.UUID:
    alice = await _alice(conv_id)
    agg = uuid.uuid4()
    pwal = uuid.uuid4()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        ind = uuid.uuid4()
        sess.add(persistent.WriteCapWAL(id=agg, write_cap=b"\x03" * 168))
        sess.add(persistent.ReadCapWAL(id=ind, write_cap_id=agg, substream_total_chunks=2))
        sess.add(persistent.PlaintextWAL(
            id=pwal, bacap_stream=conv.write_cap, conversation_id=conv_id,
            bacap_payload=b"I", indirection=ind,
        ))
        sess.add(persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=agg, conversation_id=conv_id, bacap_payload=b"C",
        ))
        sess.add(_mixwal(conv.write_cap, is_read=False, plaintextwal=pwal))
        sess.add(_mixwal(alice.read_cap_id, is_read=True))
        sess.add(persistent.SentLog(id=uuid.uuid4()))
        sess.add(persistent.TallyState(
            survey_id=uuid.uuid4().bytes, conversation_id=conv_id, doc_state=b"d",
        ))
        sess.add(persistent.PendingVoucher(
            conversation_id=conv_id, role="joiner", step="minted", voucher=b"v",
        ))
        await sess.commit()
    await _add_substream(conv_id, alice)
    await _log(conv_id, alice.id, 1, _marker(f"attachments/{conv_id}/f.bin"))
    _spill(f"attachments/{conv_id}/f.bin")
    return agg


ALL_TABLES = (
    persistent.Conversation, persistent.ConversationPeer, persistent.ConversationPeerLink,
    persistent.ConversationLog, persistent.ReadCapWAL, persistent.WriteCapWAL,
    persistent.PlaintextWAL, persistent.MixWAL, persistent.ReceivedPiece,
    persistent.TallyState, persistent.PendingVoucher,
)


@pytest.mark.asyncio
async def test_remove_conversation_leaves_nothing_behind():
    conv_id = await _make_conversation()
    await _populate_conversation(conv_id)
    sent_before = await _count(persistent.SentLog)

    await removal.remove_conversation(conversation_id=conv_id)

    for model in ALL_TABLES:
        assert await _count(model) == 0, model.__name__
    assert await _count(persistent.SentLog) == sent_before
    assert not (persistent.state_file.parent / "attachments" / str(conv_id)).exists()


@pytest.mark.asyncio
async def test_remove_conversation_leaves_other_conversations_alone():
    gone = await _make_conversation("gone")
    kept = await _make_conversation("kept")
    await _populate_conversation(gone)
    await _populate_conversation(kept)
    counts = {}
    async with persistent.asession() as sess:
        for model in ALL_TABLES:
            rows = (await sess.exec(select(model))).all()
            counts[model] = len(rows)

    await removal.remove_conversation(conversation_id=gone)

    async with persistent.asession() as sess:
        assert await sess.get(persistent.Conversation, kept) is not None
        assert await sess.get(persistent.Conversation, gone) is None
    for model in ALL_TABLES:
        assert await _count(model) * 2 == counts[model], model.__name__
    assert (persistent.state_file.parent / "attachments" / str(kept) / "f.bin").exists()


@pytest.mark.asyncio
async def test_remove_conversation_forgets_polls_in_memory():
    conv_id = await _make_conversation()
    controller = tally_controller.INSTANCE
    controller._docs[(conv_id, b"s")] = object()
    controller._docs[(conv_id + 1, b"s")] = object()
    try:
        await removal.remove_conversation(conversation_id=conv_id)
        assert (conv_id, b"s") not in controller._docs
        assert (conv_id + 1, b"s") in controller._docs
    finally:
        controller._docs.pop((conv_id + 1, b"s"), None)


@pytest.mark.asyncio
async def test_remove_conversation_announces_transfer_removal():
    conv_id = await _make_conversation()
    alice = await _alice(conv_id)
    sub = await _add_substream(conv_id, alice)

    await removal.remove_conversation(conversation_id=conv_id)

    events = []
    while not network.substream_progress_queue.empty():
        events.append(network.substream_progress_queue.get_nowait())
    assert ("removed", sub) in events


@pytest.mark.asyncio
async def test_remove_conversation_unknown_id():
    with pytest.raises(removal.RemovalError):
        await removal.remove_conversation(conversation_id=12345)
