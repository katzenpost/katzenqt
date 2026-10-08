from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QStandardItem

from katzenqt import katzen, network, persistent


async def _room(
    name: str, members: int = 2,
) -> "tuple[int, list[UUID]]":
    caps = [uuid4() for _ in range(members + 1)]
    async with persistent.asession() as sess:
        own = persistent.ConversationPeer(name="me", read_cap_id=caps[0])
        conv = persistent.Conversation(name=name)
        conv.own_peer = own
        conv.peers.append(own)
        sess.add(persistent.ReadCapWAL(id=caps[0]))
        for n, cap in enumerate(caps[1:]):
            sess.add(persistent.ReadCapWAL(id=cap))
            conv.peers.append(persistent.ConversationPeer(
                name=f"peer-{n}", read_cap_id=cap, active=True,
            ))
        sess.add(conv)
        await sess.commit()
        await sess.refresh(conv)
        return conv.id, caps[1:]


@pytest.mark.asyncio
async def test_only_the_members_streams_are_listed() -> None:
    conv_id, caps = await _room("members")
    assert sorted(
        await network._member_streams(conv_id)
    ) == sorted(caps)


@pytest.mark.asyncio
async def test_a_conversation_that_is_gone_lists_nothing() -> None:
    assert await network._member_streams(-1) == []


@pytest.mark.asyncio
async def test_a_group_pause_stops_every_member_and_resume_restarts() -> None:
    conv_id, caps = await _room("both-ways")
    assert await _paused(conv_id) is False

    await network.pause_conversation_reads(conversation_id=conv_id)
    async with persistent.asession() as sess:
        for cap in caps:
            rcw = await sess.get(persistent.ReadCapWAL, cap)
            assert rcw is not None and rcw.paused is True
    assert await _paused(conv_id) is True

    await network.resume_conversation_reads(conversation_id=conv_id)
    async with persistent.asession() as sess:
        for cap in caps:
            rcw = await sess.get(persistent.ReadCapWAL, cap)
            assert rcw is not None and rcw.paused is False
    assert await _paused(conv_id) is False


@pytest.mark.asyncio
async def test_one_member_still_read_means_the_group_is_not_paused() -> None:
    conv_id, caps = await _room("partial")
    await network.pause_peer_reads(bacap_stream=caps[0])
    assert await _paused(conv_id) is False


@pytest.mark.asyncio
async def test_a_group_with_no_members_is_not_paused() -> None:
    conv_id, _ = await _room("empty", members=0)
    assert await _paused(conv_id) is False


@pytest.mark.asyncio
async def test_the_row_is_marked_while_the_group_is_not_being_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_id, caps = await _room("marked")
    item = QStandardItem("marked")
    cast(katzen.ContactsItem, item).conversation_id = conv_id
    window = SimpleNamespace(
        ui=SimpleNamespace(contacts_treeWidget=None),
        iothread=SimpleNamespace(run_in_io=AsyncMock(side_effect=_await)),
        _remove_conversation=AsyncMock(),
        _pending_voucher=AsyncMock(return_value=None),
    )

    chosen: list[str] = []

    async def pick(menu: Any, _pos: object) -> Any:
        wanted = "Resume" if chosen else "Do not read"
        return next(
            a for a in menu.actions() if a.label.startswith(wanted)
        )

    monkeypatch.setattr(katzen, "QMenu", lambda _parent: _FakeMenu())
    monkeypatch.setattr(katzen, "_menu_chosen", pick)

    await katzen.MainWindow._conversation_menu(
        cast(katzen.MainWindow, window),
        cast(katzen.ContactsItem, item), QPoint(0, 0),
    )
    assert item.text() == katzen.paused_label("marked", True)
    assert await _paused(conv_id) is True

    chosen.append("resume")
    await katzen.MainWindow._conversation_menu(
        cast(katzen.MainWindow, window),
        cast(katzen.ContactsItem, item), QPoint(0, 0),
    )
    assert item.text() == "marked"
    assert await _paused(conv_id) is False


async def _paused(conv_id: int) -> bool:
    async with persistent.asession() as sess:
        convo = await sess.get(persistent.Conversation, conv_id)
        assert convo is not None
        own = convo.own_peer_id
    with persistent.Session(persistent._engine_sync) as sync:
        return network.conversation_reads_paused(sync, conv_id, own)


async def _await(coro: object) -> object:
    return await coro  # type: ignore[misc]


class _FakeMenu:
    def __init__(self) -> None:
        self._actions: list[Any] = []

    def addAction(self, label: str) -> Any:
        action = SimpleNamespace(label=label, enabled=True)
        action.setEnabled = lambda value, a=action: setattr(
            a, "enabled", value,
        )
        action.setCheckable = lambda value: None
        action.setChecked = lambda value: None
        self._actions.append(action)
        return action

    def addSeparator(self) -> None:
        return None

    def actions(self) -> list[Any]:
        return self._actions


@pytest.mark.asyncio
async def test_a_row_without_a_conversation_cannot_be_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    menu = _FakeMenu()
    monkeypatch.setattr(katzen, "QMenu", lambda _parent: menu)
    monkeypatch.setattr(
        katzen, "_menu_chosen", AsyncMock(return_value=None),
    )
    window = SimpleNamespace(
        ui=SimpleNamespace(contacts_treeWidget=None),
        iothread=SimpleNamespace(run_in_io=AsyncMock(side_effect=_await)),
        _remove_conversation=AsyncMock(),
        _pending_voucher=AsyncMock(return_value=None),
    )
    await katzen.MainWindow._conversation_menu(
        cast(katzen.MainWindow, window),
        cast(katzen.ContactsItem, SimpleNamespace()), QPoint(0, 0),
    )
    labelled = {a.label: a for a in menu.actions()}
    assert labelled[
        "Do not read from this group chat any more"
    ].enabled is False
    assert labelled["Resume reading from this group chat"].enabled is False
