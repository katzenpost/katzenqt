"""The GUI can re-copy a pending contact voucher instead of losing it.

`voucher_code` is a pure encoding; `pending_voucher_token` reads the joiner's
in-flight token back out of the durable PendingVoucher row so a right-click can
copy it again at any time.
"""
from __future__ import annotations

import uuid
from base64 import b64encode

import pytest

from katzenqt import persistent, voucher


def test_voucher_code_is_base64() -> None:
    token = bytes(range(24))
    assert voucher.voucher_code(token) == b64encode(token).decode()


async def _conversation(name: str = "demo") -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=uuid.uuid4(), write_cap_id=wcapwal.id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(name=name, write_cap=wcapwal.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name="me", read_cap_id=rcapwal.id, active=False, conversation=convo,
    )
    convo.own_peer = own_peer
    async with persistent.asession() as sess:
        sess.add(wcapwal)
        sess.add(rcapwal)
        sess.add(convo)
        sess.add(own_peer)
        await sess.commit()
        await sess.refresh(convo)
        return convo.id


async def _mint(conversation_id: int, role: str, token: bytes) -> None:
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role=role, conversation_id=conversation_id, step="minted", voucher=token,
        ))
        await sess.commit()


@pytest.mark.asyncio
async def test_pending_joiner_voucher_is_retrievable() -> None:
    conv = await _conversation()
    token = b"voucher-bytes-\x01\x02\x03\xff"
    await _mint(conv, "joiner", token)
    assert await voucher.pending_voucher_token(conv) == token


@pytest.mark.asyncio
async def test_no_pending_voucher_returns_none() -> None:
    conv = await _conversation()
    assert await voucher.pending_voucher_token(conv) is None


@pytest.mark.asyncio
async def test_inductor_voucher_is_not_returned_for_joiner_lookup() -> None:
    conv = await _conversation()
    await _mint(conv, "inductor", b"inductor-token")
    assert await voucher.pending_voucher_token(conv) is None
