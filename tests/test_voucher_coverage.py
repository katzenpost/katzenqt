from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from sqlmodel import select

from katzenqt import models, persistent, voucher

from tests.fakes.thinclient import FakeThinClient

if TYPE_CHECKING:
    from katzenpost_thinclient import ThinClient

_PROVISIONED_WRITE_CAP = bytes([0x09]) * 168
_MUTATED_WRITE_CAP = bytes([0x07]) * 168
_JOINER_READ_CAP = bytes([0x03]) * 136


@dataclass(frozen=True)
class _Mint:
    voucher: bytes
    voucher_write_cap: bytes
    voucher_read_cap: bytes
    voucher_secret_key: bytes
    voucher_payload: bytes


@dataclass(frozen=True)
class _Derived:
    voucher_write_cap: bytes
    voucher_read_cap: bytes


@dataclass(frozen=True)
class _Inducted:
    display_name: str
    mutated_message_read_cap: bytes
    sealed_reply: bytes


@dataclass(frozen=True)
class _Opened:
    who_reply: bytes
    mutated_message_write_cap: bytes


class MintingThinClient(FakeThinClient):
    def __init__(self) -> None:
        super().__init__()
        self.mint_calls: "list[tuple[bytes, str]]" = []
        self.payload = b"joiner voucher payload"

    async def voucher_mint(
        self,
        *,
        message_write_cap: bytes,
        display_name: str,
    ) -> _Mint:
        self.mint_calls.append((message_write_cap, display_name))
        keypair = await self.new_keypair(seed=bytes([0x11]) * 32)
        return _Mint(
            voucher=bytes([0x22]) * 32,
            voucher_write_cap=keypair.write_cap,
            voucher_read_cap=keypair.read_cap,
            voucher_secret_key=bytes([0x33]) * 32,
            voucher_payload=self.payload,
        )


class InductingConnection:
    def __init__(self) -> None:
        self.induct_calls = 0

    async def voucher_derive_stream(self, *, voucher: bytes) -> _Derived:
        return _Derived(
            voucher_write_cap=bytes([0x04]) * 168,
            voucher_read_cap=bytes([0x04]) * 136,
        )

    async def voucher_induct(
        self,
        *,
        voucher: bytes,
        voucher_payload: bytes,
        who_reply: bytes,
    ) -> _Inducted:
        self.induct_calls += 1
        return _Inducted(
            display_name="bob",
            mutated_message_read_cap=_JOINER_READ_CAP,
            sealed_reply=b"sealed reply",
        )


class OpeningConnection:
    def __init__(self, who_reply: bytes) -> None:
        self._who_reply = who_reply

    async def voucher_open(
        self,
        *,
        voucher_secret_key: bytes,
        sealed_reply: bytes,
        message_write_cap: bytes,
    ) -> _Opened:
        return _Opened(
            who_reply=self._who_reply,
            mutated_message_write_cap=_MUTATED_WRITE_CAP,
        )


async def _make_conversation(
    name: str = "demo",
    *,
    write_cap: "bytes | None" = _PROVISIONED_WRITE_CAP,
) -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(),
        write_cap=write_cap,
        next_index=None if write_cap is None else write_cap[-104:],
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        write_cap_id=wcapwal.id,
        read_cap=bytes(136),
        next_index=bytes(104),
    )
    convo = persistent.Conversation(
        name=name, write_cap=wcapwal.id, first_unread=0
    )
    own_peer = persistent.ConversationPeer(
        name="me",
        read_cap_id=rcapwal.id,
        active=False,
        conversation=convo,
    )
    convo.own_peer = own_peer
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        await sess.commit()
        await sess.refresh(convo)
        conversation_id: int = convo.id
    return conversation_id


async def _add_pending_joiner(conversation_id: int) -> None:
    async with persistent.asession() as sess:
        sess.add(
            persistent.PendingVoucher(
                role="joiner",
                conversation_id=conversation_id,
                step=voucher.STEP_AWAITING,
                voucher=bytes([0x22]) * 32,
                voucher_secret_key=bytes([0x33]) * 32,
                box1_index=bytes(104),
                voucher_read_cap=bytes(136),
            )
        )
        await sess.commit()


async def _peer_names(conversation_id: int) -> "list[str]":
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        assert conv is not None
        return sorted(
            peer.name for peer in conv.peers if peer.id != conv.own_peer_id
        )


@pytest.mark.asyncio
async def test_an_unknown_conversation_is_not_joined() -> None:
    assert await voucher.conversation_is_joined(0xFFFFFF) is False


@pytest.mark.asyncio
async def test_minting_refuses_a_conversation_without_a_write_cap() -> None:
    conversation_id = await _make_conversation(write_cap=None)
    with pytest.raises(RuntimeError, match="no provisioned write cap"):
        await voucher.mint_and_publish(
            MintingThinClient(), conversation_id, "me"
        )


@pytest.mark.asyncio
async def test_minting_publishes_box0_and_records_the_box1_index() -> None:
    client = MintingThinClient()
    keypair = await client.new_keypair(seed=bytes([0x44]) * 32)
    conversation_id = await _make_conversation(write_cap=keypair.write_cap)

    token = await voucher.mint_and_publish(client, conversation_id, "me")

    assert token == bytes([0x22]) * 32
    assert client.mint_calls == [(keypair.write_cap, "me")]

    async with persistent.asession() as sess:
        pending = (await sess.exec(select(persistent.PendingVoucher))).one()
        assert pending.step == voucher.STEP_AWAITING
        assert pending.box1_index is not None
        assert pending.voucher_write_cap is not None
        voucher_write_cap = pending.voucher_write_cap

    assert (
        client.box_store[(voucher_write_cap[32:], voucher_write_cap[-104:])]
        == client.payload
    )


@pytest.mark.asyncio
async def test_awaiting_without_a_pending_voucher_raises() -> None:
    conversation_id = await _make_conversation()
    with pytest.raises(RuntimeError, match="no pending joiner voucher"):
        await voucher.await_and_open(
            cast("ThinClient", OpeningConnection(b"")), conversation_id,
        )


@pytest.mark.asyncio
async def test_opening_caps_the_members_it_takes_from_the_reply(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id = await _make_conversation()
    await _add_pending_joiner(conversation_id)
    who_reply = models.GroupChatReplyWho(
        please_adds=[
            models.GroupChatPleaseAdd(
                display_name="alice", read_cap=bytes([0x05]) * 136
            ),
            models.GroupChatPleaseAdd(
                display_name="bob", read_cap=bytes([0x06]) * 136
            ),
        ]
    )

    async def read_box(*_a: object, **_k: object) -> "tuple[bytes, bytes]":
        return (b"sealed reply", bytes(104))

    monkeypatch.setattr(voucher, "_read_box", read_box)
    monkeypatch.setattr(voucher, "MAX_GROUP_MEMBERS", 0)

    with caplog.at_level(logging.WARNING):
        added = await voucher.await_and_open(
            cast("ThinClient", OpeningConnection(who_reply.to_cbor())),
            conversation_id,
        )

    assert added == []
    assert "capping intake at 0" in caplog.text
    assert await _peer_names(conversation_id) == []


@pytest.mark.asyncio
async def test_opening_adds_the_members_named_in_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await _make_conversation()
    await _add_pending_joiner(conversation_id)
    who_reply = models.GroupChatReplyWho(
        please_adds=[
            models.GroupChatPleaseAdd(
                display_name="alice", read_cap=bytes([0x05]) * 136
            ),
        ]
    )

    async def read_box(*_a: object, **_k: object) -> "tuple[bytes, bytes]":
        return (b"sealed reply", bytes(104))

    monkeypatch.setattr(voucher, "_read_box", read_box)

    added = await voucher.await_and_open(
        cast("ThinClient", OpeningConnection(who_reply.to_cbor())),
        conversation_id,
    )

    assert added == ["alice"]
    assert await _peer_names(conversation_id) == ["alice"]


@pytest.mark.asyncio
async def test_inducting_refuses_once_the_group_is_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id = await _make_conversation()

    async def read_box(*_a: object, **_k: object) -> "tuple[bytes, bytes]":
        return (b"voucher payload", bytes(104))

    async def publish_box(*_a: object, **_k: object) -> bytes:
        return bytes(104)

    announced: "list[tuple[int, str, bytes]]" = []

    async def send_intro(
        cid: int, display_name: str, read_cap: bytes
    ) -> None:
        announced.append((cid, display_name, read_cap))

    monkeypatch.setattr(voucher, "_read_box", read_box)
    monkeypatch.setattr(voucher, "_publish_box", publish_box)
    monkeypatch.setattr(voucher, "send_introduction_message", send_intro)
    monkeypatch.setattr(voucher, "MAX_GROUP_MEMBERS", 0)

    with caplog.at_level(logging.WARNING):
        joined = await voucher.derive_read_and_induct(
            cast("ThinClient", InductingConnection()),
            conversation_id,
            "bob",
            bytes([0x22]) * 32,
        )

    assert joined is None
    assert "reached its member limit" in caplog.text
    assert announced == []
    assert await _peer_names(conversation_id) == []


@pytest.mark.asyncio
async def test_an_unacked_introduction_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def never_sent(
        pwal_id: uuid.UUID, *, deadline_s: float, epoch_s: float,
    ) -> bool:
        return False

    monkeypatch.setattr(persistent, "wait_for_sent", never_sent)

    with caplog.at_level(logging.ERROR):
        await voucher._wait_intro_acked(uuid.uuid4(), "bob", 7)

    assert "not acked within 180s" in caplog.text
