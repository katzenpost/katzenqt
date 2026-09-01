"""Guards against ``:substream:`` peer-name spoofing.

Substream peers are distinguished from real members by a reserved name prefix
carrying the parent peer's id. A hostile member who names *themselves* with
that prefix could have their messages committed onto an arbitrary peer's log,
and a name whose id field is not an integer used to crash the read loop on
every restart. Two defences: peer-supplied names are sanitised before storage,
and the parent lookup parses defensively.
"""
from __future__ import annotations

import uuid

import pytest

from sqlmodel import select

from katzenqt import network, persistent, voucher


def test_sanitize_neutralises_the_substream_prefix() -> None:
    out = voucher._sanitize_peer_name(":substream:5:aa")
    assert not out.startswith(network._SUBSTREAM_NAME_PREFIX)
    assert out == "5:aa"


def test_sanitize_strips_nested_substream_prefixes() -> None:
    out = voucher._sanitize_peer_name(":substream::substream:9:bb")
    assert not out.startswith(network._SUBSTREAM_NAME_PREFIX)


def test_sanitize_strips_control_characters() -> None:
    out = voucher._sanitize_peer_name("a\x1b[2Jb\x07\x9bc")
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert out == "a[2Jbc"


def test_sanitize_empty_becomes_unnamed() -> None:
    assert voucher._sanitize_peer_name("") == "unnamed"
    assert voucher._sanitize_peer_name(":substream:") == "unnamed"
    assert voucher._sanitize_peer_name("\x00\x01") == "unnamed"


async def _make_conversation(name: str = "demo") -> int:
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


@pytest.mark.asyncio
async def test_add_peer_sanitises_a_hostile_name() -> None:
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        voucher._add_peer(sess, conv, ":substream:5:aa", b"\x02" * 136)
        await sess.commit()
    async with persistent.asession() as sess:
        names = [
            p.name for p in (await sess.exec(
                select(persistent.ConversationPeer)
            )).all()
        ]
    assert not any(n.startswith(network._SUBSTREAM_NAME_PREFIX) for n in names)


@pytest.mark.asyncio
async def test_substream_parent_rejects_a_non_integer_id() -> None:
    async with persistent.asession() as sess:
        assert await network._substream_parent(sess, ":substream:xx:aa") is None


@pytest.mark.asyncio
async def test_substream_parent_rejects_a_truncated_name() -> None:
    async with persistent.asession() as sess:
        assert await network._substream_parent(sess, ":substream:") is None


@pytest.mark.asyncio
async def test_substream_parent_returns_none_for_a_missing_parent() -> None:
    async with persistent.asession() as sess:
        assert await network._substream_parent(sess, ":substream:9999:aa") is None


@pytest.mark.asyncio
async def test_substream_parent_resolves_an_existing_peer() -> None:
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        peer = (await sess.exec(
            select(persistent.ConversationPeer)
        )).first()
        parent = await network._substream_parent(
            sess, f":substream:{peer.id}:aa",
        )
        assert parent is not None
        assert parent.id == peer.id
