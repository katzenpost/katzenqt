"""Unit tests for conversation_handlers._already_has and the self-
recognition fallback in _handle_introduction.

Uses raw random bytes for caps: these tests are about DB/identity shape,
not real BACAP crypto.
"""
import secrets
import uuid

import pytest

from katzenqt import conversation_handlers, models, persistent


def _read_cap() -> bytes:
    return secrets.token_bytes(136)


def _write_cap() -> bytes:
    return secrets.token_bytes(168)


def _index() -> bytes:
    return secrets.token_bytes(104)


async def _make_conversation(sess, *, own_name="self", own_write_cap=None):
    """A conversation with just its own (inactive) peer, mirroring
    new_conversation's shape. Returns (conversation_id, own peer's
    ConversationPeer.id)."""
    wcw_id = uuid.uuid4()
    rcw_id = uuid.uuid4()
    own_read_cap = _read_cap()
    sess.add(persistent.WriteCapWAL(id=wcw_id, write_cap=own_write_cap, next_index=_index()))
    sess.add(persistent.ReadCapWAL(id=rcw_id, write_cap_id=wcw_id, read_cap=own_read_cap, next_index=_index()))
    own_peer = persistent.ConversationPeer(name=own_name, read_cap_id=rcw_id, active=False)
    sess.add(own_peer)
    await sess.commit()
    await sess.refresh(own_peer)
    own_peer_id = own_peer.id
    conv = persistent.Conversation(name="demo", own_peer_id=own_peer_id, write_cap=wcw_id)
    sess.add(conv)
    await sess.commit()
    await sess.refresh(conv)
    conv_id = conv.id
    sess.add(persistent.ConversationPeerLink(conversation_peer_id=own_peer_id, conversation_id=conv_id))
    await sess.commit()
    return conv_id, own_peer_id, own_read_cap


async def _add_active_peer(sess, conv_id: int, *, name: str, read_cap: bytes):
    rcw_id = uuid.uuid4()
    sess.add(persistent.ReadCapWAL(id=rcw_id, read_cap=read_cap, next_index=_index()))
    peer = persistent.ConversationPeer(name=name, read_cap_id=rcw_id, active=True)
    sess.add(peer)
    await sess.commit()
    await sess.refresh(peer)
    sess.add(persistent.ConversationPeerLink(conversation_peer_id=peer.id, conversation_id=conv_id))
    await sess.commit()


class TestAlreadyHas:
    @pytest.mark.asyncio
    async def test_distinct_read_cap_with_same_name_is_not_already_had(self):
        # Two different people who happen to pick the same display name
        # must both be addable; a name match alone must never suppress a
        # genuinely distinct peer.
        async with persistent.asession() as sess:
            conv_id, _own_id, _own_rc = await _make_conversation(sess)
            await _add_active_peer(sess, conv_id, name="alice", read_cap=_read_cap())
            intro = models.GroupChatPleaseAdd(display_name="alice", read_cap=_read_cap())
            assert await conversation_handlers._already_has(sess, conv_id, intro) is False

    @pytest.mark.asyncio
    async def test_same_read_cap_is_already_had(self):
        async with persistent.asession() as sess:
            conv_id, _own_id, _own_rc = await _make_conversation(sess)
            rc = _read_cap()
            await _add_active_peer(sess, conv_id, name="alice", read_cap=rc)
            intro = models.GroupChatPleaseAdd(display_name="someone else's name for them", read_cap=rc)
            assert await conversation_handlers._already_has(sess, conv_id, intro) is True


class TestHandleIntroductionSelfRecognition:
    @pytest.mark.asyncio
    async def test_falls_back_to_own_read_cap_when_write_cap_unprovisioned(self):
        # own_peer's write cap hasn't been provisioned yet (write_cap=None);
        # an announcement carrying our own (unmutated) read cap must still
        # be recognised as "about ourselves" via the read-cap fallback,
        # rather than being added as a peer of our own conversation.
        async with persistent.asession() as sess:
            conv_id, own_peer_id, own_read_cap = await _make_conversation(
                sess, own_write_cap=None,
            )
            own_peer = await sess.get(persistent.ConversationPeer, own_peer_id)
            gcm = models.GroupChatMessage(
                version=0, membership_hash=b"m" * 32,
                msg_type=models.GroupChatTypeEnum.INTRODUCTION,
                introduction=models.GroupChatPleaseAdd(
                    display_name="self", read_cap=own_read_cap,
                ),
            )
            added, _signal, peer_added = await conversation_handlers._handle_introduction(
                sess, own_peer, gcm, b"F" + gcm.to_cbor(),
            )
            assert added is True  # the message itself is still logged
            assert peer_added is None  # no self-add happened
            links = (await sess.exec(
                persistent.select(persistent.ConversationPeerLink).where(
                    persistent.ConversationPeerLink.conversation_id == conv_id
                )
            )).all()
            # Still just the one (own) peer link: no self-add happened.
            assert len(links) == 1

    @pytest.mark.asyncio
    async def test_returns_peer_added_for_the_caller_to_announce(self):
        # peer_added must be returned (not fired as a notification here):
        # the caller only announces it after its own commit succeeds, so a
        # retried transaction can't duplicate the notification.
        async with persistent.asession() as sess:
            conv_id, own_peer_id, _own_rc = await _make_conversation(sess)
            own_peer = await sess.get(persistent.ConversationPeer, own_peer_id)
            newcomer_rc = _read_cap()
            gcm = models.GroupChatMessage(
                version=0, membership_hash=b"m" * 32,
                msg_type=models.GroupChatTypeEnum.INTRODUCTION,
                introduction=models.GroupChatPleaseAdd(
                    display_name="carol", read_cap=newcomer_rc,
                ),
            )
            added, _signal, peer_added = await conversation_handlers._handle_introduction(
                sess, own_peer, gcm, b"F" + gcm.to_cbor(),
            )
            assert added is True
            assert peer_added == (conv_id, "carol")
