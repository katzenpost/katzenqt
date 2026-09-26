import pytest
from sqlmodel import select

from katzenqt import persistent
from katzenqt.headless import _actions, _args
from tests.test_membership_hash import _make_conversation


async def _peer_names() -> "list[str]":
    async with persistent.asession() as sess:
        return sorted(
            p.name
            for p in (
                await sess.exec(select(persistent.ConversationPeer))
            ).all()
        )


@pytest.mark.asyncio
async def test_remove_peer_action_deletes_the_named_member():
    await _make_conversation("room")

    code = await _actions._action_remove_peer(
        _args.RemovePeer(
            action="remove-peer", conv_name="room", peer_name="alice"
        ),
    )

    assert code == 0
    assert await _peer_names() == ["me"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conv,peer", [("room", "nobody"), ("room", "me"), ("ghost", "alice")]
)
async def test_remove_peer_action_refuses_and_changes_nothing(conv, peer):
    await _make_conversation("room")

    code = await _actions._action_remove_peer(
        _args.RemovePeer(
            action="remove-peer", conv_name=conv, peer_name=peer
        ),
    )

    assert code == 2
    assert await _peer_names() == ["alice", "me"]


@pytest.mark.asyncio
async def test_remove_conv_action_deletes_only_that_conversation():
    await _make_conversation("gone")
    await _make_conversation("kept")

    code = await _actions._action_remove_conv(
        _args.RemoveConv(action="remove-conv", conv_name="gone"),
    )

    assert code == 0
    async with persistent.asession() as sess:
        names = [
            c.name
            for c in (await sess.exec(select(persistent.Conversation))).all()
        ]
    assert names == ["kept"]


@pytest.mark.asyncio
async def test_remove_conv_action_unknown_name():
    code = await _actions._action_remove_conv(
        _args.RemoveConv(action="remove-conv", conv_name="ghost"),
    )
    assert code == 2
