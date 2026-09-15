""":substream: peers must never surface in user-facing lists.

- katzen._peer_is_displayable keeps synthetic substream peers out of the
  contacts tree (add_conversation, _process_peer_added, voucher inductors).
- voucher._build_who_reply must not offer a substream peer for a newcomer
  to add.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from katzenqt import katzen, persistent, voucher
from katzenqt.network import _SUBSTREAM_NAME_PREFIX


def _peer(name: str, *, active: bool = True, peer_id: int = 1,
           read_cap_id: "uuid.UUID | None" = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, active=active, id=peer_id,
        read_cap_id=read_cap_id or uuid.uuid4(),
    )


class TestPeerIsDisplayable:
    def test_normal_peer_is_displayable(self):
        assert katzen._peer_is_displayable(_peer("alice")) is True

    def test_substream_peer_is_not_displayable(self):
        assert katzen._peer_is_displayable(
            _peer(f"{_SUBSTREAM_NAME_PREFIX}<demo>:<nonce>")
        ) is False

    def test_inactive_substream_peer_is_not_displayable(self):
        assert katzen._peer_is_displayable(
            _peer(f"{_SUBSTREAM_NAME_PREFIX}<demo>:<nonce>", active=False)
        ) is False

    def test_looks_like_substream_but_exact_prefix_matters(self):
        # A real peer named e.g. ":substreams!" must survive: only the exact
        # reserved prefix is filtered.
        assert katzen._peer_is_displayable(_peer(":substreams!")) is True


async def _make_conversation(*, own_read_cap: "bytes | None" = b"\x00" * 136) -> int:
    wcapwal = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
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


async def _add_active_peer(conversation_id: int, name: str) -> None:
    rcw = persistent.ReadCapWAL(
        id=uuid.uuid4(), read_cap=b"\x01" * 136, next_index=b"\x01" * 104,
    )
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conversation_id)
        sess.add(rcw)
        sess.add(persistent.ConversationPeer(
            name=name, read_cap_id=rcw.id, active=True, conversation=conv,
        ))
        await sess.commit()


class TestBuildWhoReplySkipsSubstreamPeers:
    @pytest.mark.asyncio
    async def test_active_substream_peer_is_omitted(self):
        conv_id = await _make_conversation()
        await _add_active_peer(conv_id, "alice")
        await _add_active_peer(conv_id, f"{_SUBSTREAM_NAME_PREFIX}<demo>:<nonce>")
        reply = await voucher._build_who_reply(conv_id)
        names = {p.display_name for p in reply.please_adds}
        assert "alice" in names
        assert f"{_SUBSTREAM_NAME_PREFIX}<demo>:<nonce>" not in names

    @pytest.mark.asyncio
    async def test_only_normal_peer_survives(self):
        conv_id = await _make_conversation()
        await _add_active_peer(conv_id, "bob")
        reply = await voucher._build_who_reply(conv_id)
        assert sorted(p.display_name for p in reply.please_adds) == ["bob", "me"]