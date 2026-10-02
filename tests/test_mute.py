from __future__ import annotations

from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from katzenqt import katzen, persistent


@pytest.mark.asyncio
async def test_mute_persists_and_unmute_leaves_no_row() -> None:
    assert await persistent.is_muted(11) is False
    await persistent.set_muted(11, muted=True)
    assert await persistent.is_muted(11) is True
    assert await persistent.is_muted(12) is False
    await persistent.set_muted(11, muted=True)
    assert await persistent.is_muted(11) is True
    await persistent.set_muted(11, muted=False)
    assert await persistent.is_muted(11) is False
    async with persistent.asession() as sess:
        assert await sess.get(
            persistent.AppSetting, persistent.muted_key(11),
        ) is None


@pytest.mark.asyncio
async def test_unmuting_a_conversation_never_muted_is_harmless() -> None:
    await persistent.set_muted(13, muted=False)
    assert await persistent.is_muted(13) is False


class _Action:
    def __init__(self, name: str) -> None:
        self.name = name
        self.checkable = False
        self.checked = False
        self.enabled = True

    def setCheckable(self, state: bool) -> None:
        self.checkable = state

    def setChecked(self, state: bool) -> None:
        self.checked = state

    def setEnabled(self, state: bool) -> None:
        self.enabled = state


class _Menu:
    def __init__(self, parent: object) -> None:
        self.actions: "dict[str, _Action]" = {}
        self.order: "list[str]" = []

    def addAction(self, name: str) -> _Action:
        self.actions[name] = _Action(name)
        self.order.append(name)
        return self.actions[name]

    def addSeparator(self) -> None:
        self.order.append("-")


def _window(
    monkeypatch: pytest.MonkeyPatch, pick: "str | None",
) -> "tuple[Any, list[_Menu]]":
    menus: "list[_Menu]" = []

    def menu(parent: object) -> _Menu:
        menus.append(_Menu(parent))
        return menus[-1]

    async def chosen(api: _Menu, pos: object) -> "_Action | None":
        return api.actions[pick] if pick is not None else None

    async def run(coroutine: "Coroutine[object, object, Any]") -> Any:
        return await coroutine

    monkeypatch.setattr(katzen, "QMenu", menu)
    monkeypatch.setattr(katzen, "_menu_chosen", chosen)
    window = SimpleNamespace(
        ui=SimpleNamespace(contacts_treeWidget=object()),
        iothread=SimpleNamespace(run_in_io=run),
        _remove_conversation=AsyncMock(),
    )
    return window, menus


@pytest.mark.asyncio
async def test_the_menu_offers_a_checked_box_for_a_muted_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await persistent.set_muted(21, muted=True)
    window, menus = _window(monkeypatch, None)
    await katzen.MainWindow._conversation_menu(
        window, cast(Any, SimpleNamespace(conversation_id=21)),
        cast(Any, None),
    )
    mute = menus[0].actions["Mute notifications"]
    assert menus[0].order[0] == "Mute notifications"
    assert (mute.checkable, mute.checked, mute.enabled) == (True, True, True)


@pytest.mark.asyncio
async def test_choosing_the_entry_toggles_the_stored_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(monkeypatch, "Mute notifications")
    item = cast(Any, SimpleNamespace(conversation_id=22))
    await katzen.MainWindow._conversation_menu(window, item, cast(Any, None))
    assert await persistent.is_muted(22) is True
    await katzen.MainWindow._conversation_menu(window, item, cast(Any, None))
    assert await persistent.is_muted(22) is False
    window._remove_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_row_without_a_conversation_cannot_be_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, menus = _window(monkeypatch, "Mute notifications")
    await katzen.MainWindow._conversation_menu(
        window, cast(Any, SimpleNamespace()), cast(Any, None),
    )
    assert menus[0].actions["Mute notifications"].enabled is False


@pytest.mark.asyncio
async def test_removal_is_still_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(monkeypatch, "Remove group chat...")
    item = cast(Any, SimpleNamespace(conversation_id=23))
    await katzen.MainWindow._conversation_menu(window, item, cast(Any, None))
    window._remove_conversation.assert_awaited_once_with(item)
