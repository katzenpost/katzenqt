from __future__ import annotations

from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from PySide6.QtCore import QPoint

from katzenqt import katzen, persistent, voucher


def test_a_voucher_code_is_its_base64_text() -> None:
    assert voucher.voucher_code(b"\x00\xff") == "AP8="


@pytest.mark.asyncio
async def test_only_a_pending_joiner_voucher_is_offered() -> None:
    async with persistent.asession() as sess:
        sess.add(persistent.PendingVoucher(
            role="joiner", conversation_id=7, voucher=b"tok",
            step=voucher.STEP_MINTED,
        ))
        sess.add(persistent.PendingVoucher(
            role="inductor", conversation_id=8, voucher=b"other",
            step=voucher.STEP_MINTED,
        ))
        await sess.commit()
    assert await voucher.pending_voucher_token(7) == b"tok"
    assert await voucher.pending_voucher_token(8) is None
    assert await voucher.pending_voucher_token(9) is None


class _Action:
    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = True
        self.checked = False

    def setEnabled(self, state: bool) -> None:
        self.enabled = state

    def setCheckable(self, state: bool) -> None:
        pass

    def setChecked(self, state: bool) -> None:
        self.checked = state


class _Menu:
    def __init__(self, parent: object) -> None:
        self.actions: "dict[str, _Action]" = {}
        self.order: "list[str]" = []

    def addAction(self, name: str) -> _Action:
        action = _Action(name)
        self.actions[name] = action
        self.order.append(name)
        return action

    def addSeparator(self) -> None:
        self.order.append("-")


class _Harness:
    def __init__(self) -> None:
        self.menu: "_Menu | None" = None
        self.messages: "list[str]" = []
        self.copied: "list[str]" = []
        self.shown: "list[str]" = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: "bytes | None",
    pick: "str | None",
    conversation_id: "int | None" = 7,
) -> "tuple[Any, _Harness]":
    harness = _Harness()

    def menu(parent: object) -> _Menu:
        harness.menu = _Menu(parent)
        return harness.menu

    async def chosen(api: _Menu, pos: object) -> "_Action | None":
        return api.actions[pick] if pick is not None else None

    async def run(coroutine: "Coroutine[object, object, Any]") -> Any:
        return await coroutine

    async def token_for(wanted: int) -> "bytes | None":
        assert wanted == conversation_id
        return token

    async def dialog_finished(dialog: object) -> bool:
        harness.shown.append(getattr(dialog, "code", ""))
        return True

    monkeypatch.setattr(katzen, "QMenu", menu)
    monkeypatch.setattr(katzen, "_menu_chosen", chosen)
    monkeypatch.setattr(katzen, "pending_voucher_token", token_for)
    monkeypatch.setattr(katzen, "_dialog_finished", dialog_finished)
    monkeypatch.setattr(
        katzen, "VoucherDialog",
        lambda parent, code: SimpleNamespace(code=code),
    )
    monkeypatch.setattr(
        katzen, "QApplication",
        SimpleNamespace(clipboard=lambda: SimpleNamespace(
            setText=harness.copied.append,
        )),
    )
    window = SimpleNamespace(
        ui=SimpleNamespace(
            contacts_treeWidget=object(),
            statusbar=SimpleNamespace(
                showMessage=lambda text, ms: harness.messages.append(text),
            ),
        ),
        iothread=SimpleNamespace(run_in_io=run),
        _remove_conversation=AsyncMock(),
    )
    window._pending_voucher = lambda item: (
        katzen.MainWindow._pending_voucher(
            cast("katzen.MainWindow", window), item,
        )
    )
    return window, harness


def _item(conversation_id: "int | None") -> Any:
    item = SimpleNamespace()
    if conversation_id is not None:
        item.conversation_id = conversation_id
    return item


@pytest.mark.asyncio
async def test_copying_puts_the_voucher_on_the_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, harness = _install(
        monkeypatch, token=b"tok", pick="Copy voucher",
    )
    await katzen.MainWindow._conversation_menu(window, _item(7), QPoint())
    assert harness.copied == ["dG9r"]
    assert harness.messages == ["Voucher copied to clipboard"]
    assert harness.shown == []
    window._remove_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_showing_reopens_the_dialog_with_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, harness = _install(
        monkeypatch, token=b"tok", pick="Show voucher...",
    )
    await katzen.MainWindow._conversation_menu(window, _item(7), QPoint())
    assert harness.shown == ["dG9r"]
    assert harness.copied == []


@pytest.mark.asyncio
async def test_both_entries_are_disabled_without_a_pending_voucher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, harness = _install(monkeypatch, token=None, pick=None)
    await katzen.MainWindow._conversation_menu(window, _item(7), QPoint())
    assert harness.menu is not None
    assert harness.menu.order[-1] == "Remove group chat..."
    assert [
        n for n in harness.menu.order if "voucher" in n.lower()
    ] == ["Copy voucher", "Show voucher..."]
    assert harness.menu.actions["Copy voucher"].enabled is False
    assert harness.menu.actions["Show voucher..."].enabled is False
    assert harness.menu.actions["Remove group chat..."].enabled is True


@pytest.mark.asyncio
async def test_a_row_without_a_conversation_id_offers_no_voucher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, harness = _install(
        monkeypatch, token=b"tok", pick=None, conversation_id=None,
    )
    await katzen.MainWindow._conversation_menu(window, _item(None), QPoint())
    assert harness.menu is not None
    assert harness.menu.actions["Copy voucher"].enabled is False


@pytest.mark.asyncio
async def test_removal_still_works_with_a_voucher_outstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, harness = _install(
        monkeypatch, token=b"tok", pick="Remove group chat...",
    )
    item = _item(7)
    await katzen.MainWindow._conversation_menu(window, item, QPoint())
    window._remove_conversation.assert_awaited_once_with(item)
    assert harness.copied == []
    assert harness.shown == []
