"""Unit tests for a handful of voucher.py helper-level fixes:

- _add_peer refuses a malformed/missing read_cap instead of crashing.
- send_introduction_message never raises: a failure writing the
  announcement is logged, not propagated to the caller.
- _build_who_reply omits itself, rather than sending a broken entry,
  when its own read cap isn't provisioned yet.
"""
from __future__ import annotations

import uuid

import pytest

from katzenqt import persistent, voucher


async def _make_conversation(*, own_write_cap: "bytes | None" = b"\x00" * 168,
                              own_read_cap: "bytes | None" = b"\x00" * 136) -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=own_write_cap, next_index=b"\x00" * 104,
    )
    rcapwal = persistent.ReadCapWAL(
        id=wcapwal.id, write_cap_id=wcapwal.id,
        read_cap=own_read_cap, next_index=b"\x00" * 104,
    )
    convo = persistent.Conversation(name="demo", write_cap=wcapwal.id, first_unread=0)
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


class TestAddPeerGuard:
    @pytest.mark.asyncio
    async def test_none_read_cap_does_not_raise_and_adds_nothing(self):
        async with persistent.asession() as sess:
            conv_id = await _make_conversation()
            conv = await sess.get(persistent.Conversation, conv_id)
            voucher._add_peer(sess, conv, "newcomer", None)
            await sess.commit()
            peers = (await sess.exec(
                persistent.select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.name == "newcomer"
                )
            )).all()
            assert peers == []

    @pytest.mark.asyncio
    async def test_wrong_length_read_cap_does_not_raise_and_adds_nothing(self):
        async with persistent.asession() as sess:
            conv_id = await _make_conversation()
            conv = await sess.get(persistent.Conversation, conv_id)
            voucher._add_peer(sess, conv, "newcomer", b"\x00" * 10)
            await sess.commit()
            peers = (await sess.exec(
                persistent.select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.name == "newcomer"
                )
            )).all()
            assert peers == []


class TestBuildWhoReplyOmitsUnprovisionedSelf:
    @pytest.mark.asyncio
    async def test_omits_self_when_neither_cap_is_provisioned(self):
        conv_id = await _make_conversation(own_write_cap=None, own_read_cap=None)
        reply = await voucher._build_who_reply(conv_id)
        assert reply.please_adds == []

    @pytest.mark.asyncio
    async def test_includes_self_when_read_cap_is_provisioned(self):
        conv_id = await _make_conversation(own_write_cap=None, own_read_cap=b"\x02" * 136)
        reply = await voucher._build_who_reply(conv_id)
        assert len(reply.please_adds) == 1
        assert reply.please_adds[0].read_cap == b"\x02" * 136


class TestSendIntroductionMessageNeverRaises:
    @pytest.mark.asyncio
    async def test_write_failure_is_logged_not_raised(self, monkeypatch, caplog):
        async def boom(*_a, **_kw):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(voucher, "_write_introduction_log", boom)
        # Must not raise.
        await voucher.send_introduction_message(1, "carol", b"\x00" * 136)
        assert any(
            "failed to write INTRODUCTION" in r.message for r in caplog.records
        )


class TestOwnReadCapDedupe:
    """The single own-read-cap lookup now shared by _handle_introduction
    and _build_who_reply: write-cap-derived once provisioned, else the
    unmutated rcapwal.read_cap."""

    @pytest.mark.asyncio
    async def test_prefers_provisioned_write_cap(self):
        conv_id = await _make_conversation(
            own_write_cap=b"\xaa" * 168, own_read_cap=b"\xbb" * 136,
        )
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conv_id)
            assert await persistent.own_read_cap(sess, conv) == b"\xaa" * 136

    @pytest.mark.asyncio
    async def test_falls_back_to_read_cap_when_write_cap_unprovisioned(self):
        conv_id = await _make_conversation(
            own_write_cap=None, own_read_cap=b"\xbb" * 136,
        )
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conv_id)
            assert await persistent.own_read_cap(sess, conv) == b"\xbb" * 136

    @pytest.mark.asyncio
    async def test_none_when_neither_is_provisioned(self):
        conv_id = await _make_conversation(own_write_cap=None, own_read_cap=None)
        async with persistent.asession() as sess:
            conv = await sess.get(persistent.Conversation, conv_id)
            assert await persistent.own_read_cap(sess, conv) is None
