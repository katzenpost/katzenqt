"""A scan, which only the user starts: it jumps a member's stream forward
to the furthest box other members acknowledged, keeping the boxes it
stepped past, and reads those again on a later scan."""

from __future__ import annotations

import struct
import uuid
from typing import cast

import pytest
from sqlmodel import col, select

from katzenqt import ack_codec, acks, conversation_handlers, models, network, persistent
from katzenqt.groupchat.events import Walked
from tests.fakes.thinclient import FakeThinClient
from tests.test_acks import _Chat, _text


def _box(n: int) -> bytes:
    return struct.pack("<Q", n) + bytes(96)


BOB_CAP = bytes([2]) * 32 + _box(0)
CAROL_CAP = bytes([3]) * 32 + _box(0)


async def _receive(chat: _Chat, name: str, gcm: models.GroupChatMessage, n: int) -> None:
    peer_id, rcw_id, _ = chat.peers[name]
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _box(n + 1)
        await conversation_handlers.dispatch(sess, peer, gcm, b"F" + gcm.to_cbor(), box=_box(n))
        await acks.box_read(sess, peer, rcw, _box(n))
        await sess.commit()


async def _stuck() -> tuple[_Chat, FakeThinClient]:
    """We read bob's first box; his second is gone, and carol has read up
    to his third."""
    chat = await _Chat().create()
    bob = await chat.add_peer("bob", BOB_CAP)
    await chat.add_peer("carol", CAROL_CAP)
    await chat.found("bob", "carol")
    await _receive(chat, "bob", _text("one"), 0)
    at = (await chat.group()).rosters.index_of(bytes([3]) * 32, bob)
    assert at is not None
    await _receive(chat, "carol", models.GroupChatMessage(
        version=models.GROUP_CHAT_VERSION, text="read", acks=ack_codec.encode({at: _box(2)}),
    ), 0)
    client = FakeThinClient()
    client.box_store[BOB_CAP, _box(2)] = b"F" + _text("three").to_cbor()
    return chat, client


async def _scan(chat: _Chat, client: FakeThinClient) -> network.ScanResult | None:
    return await network.scan_member(
        cast(network.ThinClient, client),
        conversation_id=chat.conversation_id, peer_id=chat.peers["bob"][0],
    )


async def _next_index(chat: _Chat) -> bytes | None:
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, chat.peers["bob"][1])
        assert rcw is not None
        return rcw.next_index


async def _texts(chat: _Chat) -> list[str | None]:
    async with persistent.asession() as sess:
        rows = (await sess.exec(
            select(persistent.ConversationLog)
            .where(persistent.ConversationLog.conversation_id == chat.conversation_id)
            .order_by(col(persistent.ConversationLog.conversation_order))
        )).all()
    return [models.GroupChatMessage.from_cbor(r.payload[1:]).text for r in rows]


@pytest.mark.asyncio
async def test_a_scan_jumps_to_the_box_another_member_acknowledged() -> None:
    chat, client = await _stuck()
    assert await _scan(chat, client) == network.ScanResult(0, True, 1)
    assert await _next_index(chat) == _box(2)
    async with persistent.asession() as sess:
        stream = await acks.stream(sess, chat.conversation_id, bytes([2]) * 32)
    assert stream == acks.Stream(_box(2), 1, False)


@pytest.mark.asyncio
async def test_a_later_scan_reads_a_box_stepped_past_once_it_is_rewritten() -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    assert await _scan(chat, client) == network.ScanResult(0, False, 1)
    client.box_store[BOB_CAP, _box(1)] = b"F" + _text("two").to_cbor()
    assert await _scan(chat, client) == network.ScanResult(1, False, 0)
    assert "two" in await _texts(chat)


@pytest.mark.asyncio
async def test_a_tombstone_stepped_past_counts_as_read() -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    client.inject_error("start_resending_encrypted_message", network.TombstoneError("tombstone"))
    assert await _scan(chat, client) == network.ScanResult(1, False, 0)


@pytest.mark.asyncio
async def test_a_box_announcing_a_file_is_left_for_later() -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    client.box_store[BOB_CAP, _box(1)] = b"I" + bytes(136)
    assert await _scan(chat, client) == network.ScanResult(0, False, 1)


@pytest.mark.asyncio
async def test_a_box_that_cannot_be_read_still_counts_as_read() -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    client.box_store[BOB_CAP, _box(1)] = b"F\xff"
    assert await _scan(chat, client) == network.ScanResult(1, False, 0)


@pytest.mark.asyncio
async def test_a_scan_with_nothing_acknowledged_ahead_stays_put() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob", BOB_CAP)
    await chat.found("bob")
    assert await _scan(chat, FakeThinClient()) == network.ScanResult(0, False, 0)
    assert await _next_index(chat) == _box(0)


@pytest.mark.asyncio
async def test_an_acknowledged_box_too_far_ahead_is_not_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acks, "WALK_LIMIT", 1)
    chat, client = await _stuck()
    assert await _scan(chat, client) == network.ScanResult(0, False, 0)
    assert await _next_index(chat) == _box(1)


@pytest.mark.asyncio
async def test_a_conversation_without_a_group_has_nothing_to_scan() -> None:
    chat = await _Chat().create()
    await chat.add_peer("bob", BOB_CAP)
    assert await _scan(chat, FakeThinClient()) is None


@pytest.mark.asyncio
async def test_an_unknown_member_has_nothing_to_scan() -> None:
    chat = await _Chat().create()
    assert await network.scan_member(
        cast(network.ThinClient, FakeThinClient()),
        conversation_id=chat.conversation_id, peer_id=999,
    ) is None


@pytest.mark.asyncio
async def test_a_paused_member_stays_paused() -> None:
    chat, client = await _stuck()
    await network.pause_peer_reads(bacap_stream=chat.peers["bob"][1])
    await _scan(chat, client)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, chat.peers["bob"][1])
        assert rcw is not None and rcw.paused


@pytest.mark.asyncio
async def test_group_info_says_what_is_known_of_each_stream() -> None:
    chat, _ = await _stuck()
    await chat.add_peer("dave")
    info = await network.group_info(conversation_id=chat.conversation_id)
    assert info is not None
    assert sorted(info.roster) == ["bob", "carol", "me"]
    assert sorted(info.members) == [
        ("bob", None, "Others have read further"),
        ("carol", None, "Nothing known to be missing"),
        ("dave", None, "Not read as a member"),
    ]


@pytest.mark.asyncio
async def test_group_info_needs_a_group() -> None:
    chat = await _Chat().create()
    assert await network.group_info(conversation_id=chat.conversation_id) is None
    assert await network.group_info(conversation_id=999) is None


@pytest.mark.asyncio
async def test_the_stream_of_an_unknown_member_is_unknown() -> None:
    chat, _ = await _stuck()
    assert await network.member_stream(
        conversation_id=chat.conversation_id, read_cap_id=uuid.uuid4(),
    ) is None
    assert await network.member_stream(
        conversation_id=chat.conversation_id, read_cap_id=chat.peers["bob"][1],
    ) == acks.Stream(_box(1), 0, True)


@pytest.mark.asyncio
async def test_a_small_file_stepped_past_is_left_for_later() -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    upload = models.GroupChatMessage(
        version=models.GROUP_CHAT_VERSION,
        msg_type=models.GroupChatTypeEnum.FILE_UPLOAD,
        file_upload=models.GroupChatFileUpload(payload=b"x", filetype="arbitrary", basename="x"),
    )
    client.box_store[BOB_CAP, _box(1)] = b"F" + upload.to_cbor()
    assert await _scan(chat, client) == network.ScanResult(0, False, 1)


@pytest.mark.asyncio
async def test_what_a_message_stepped_past_announces_is_passed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, client = await _stuck()
    await _scan(chat, client)
    client.box_store[BOB_CAP, _box(1)] = b"F" + _text("two").to_cbor()
    announced = conversation_handlers.PeerAnnouncement(chat.conversation_id, "erin")

    async def dispatch(*args: object, **kwargs: object) -> conversation_handlers.DispatchResult:
        return conversation_handlers.DispatchResult(False, False, announced, True)

    monkeypatch.setattr(conversation_handlers, "dispatch", dispatch)
    await _scan(chat, client)
    assert network.peer_added_queue.get_nowait() == announced
    assert network.tally_update_queue.get_nowait() == chat.conversation_id


@pytest.mark.asyncio
async def test_a_scan_step_without_a_group_does_nothing() -> None:
    chat = await _Chat().create()
    async with persistent.asession() as sess:
        assert await acks.scanned(
            sess, chat.conversation_id, Walked(bytes(32), None, ()),
        ) == []
