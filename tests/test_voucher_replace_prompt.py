"""The "a voucher is already pending" replace prompt must not crash.

``generate_voucher`` awaits a Yes/No ``QMessageBox`` and then decides with the
value ``_dialog_finished`` returned. That value is the result code passed to
``QDialog.finished`` (for a ``QMessageBox``, the clicked ``StandardButton``),
*not* a button widget, so ``QMessageBox.standardButton(result)`` raised
``TypeError`` on every click and the abandon-and-remint path never worked.

These stubbed, loop-bound tests (no real Qt) pin that clicking No returns
without cancelling anything, and clicking Yes cancels the pending voucher and
proceeds to the name prompt.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine, Iterator
from types import SimpleNamespace
from typing import TypeVar

import pytest

from katzenqt import katzen

_T = TypeVar("_T")


class _StubMsgBox:
    """Stand-in for QMessageBox exposing only what generate_voucher touches.

    ``standardButton`` deliberately raises: the production code must decide
    from the result code, never by calling it.
    """

    class Icon:
        Question = 0

    class StandardButton:
        Yes = 0x4000
        No = 0x10000

    def __init__(self, *a: object, **k: object) -> None:
        pass

    def setStandardButtons(self, *a: object) -> None:
        pass

    def setDefaultButton(self, *a: object) -> None:
        pass

    def standardButton(self, *a: object) -> None:
        raise AssertionError(
            "standardButton() maps a button widget, not a result code"
        )


class _Loop:
    """Stand-in for the io thread: runs the queued coroutine on this loop."""

    async def run_in_io(self, fn: Coroutine[object, object, _T]) -> _T:
        if not asyncio.iscoroutine(fn):
            raise TypeError("A coroutine object is required")
        return await fn


class _Convo:
    conversation_id = 7
    own_peer_name = "me"


def _fake_window() -> SimpleNamespace:
    return SimpleNamespace(convo_state=lambda: _Convo(), iothread=_Loop())


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch, dialog_results: Iterator[int],
) -> tuple[list[uuid.UUID], list[tuple[tuple[object, ...], dict[str, object]]]]:
    """Stub the Qt dialog surface and the voucher helpers.

    ``dialog_results`` is an iterator of the values successive
    ``_dialog_finished`` calls return. Returns ``(cancel_calls, mint_calls)``.
    """
    cancel_calls: list[uuid.UUID] = []
    mint_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _InputDialog:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def setWindowTitle(self, *a: object) -> None:
            pass

        def setLabelText(self, *a: object) -> None:
            pass

        def textValue(self) -> str:
            return ""

    async def _not_joined(_conversation_id: int) -> bool:
        return False

    async def _pending(_conversation_id: int) -> uuid.UUID:
        return uuid.uuid4()

    async def _dialog_finished(_dialog: object) -> int:
        return next(dialog_results)

    async def _cancel(pv_id: uuid.UUID) -> None:
        cancel_calls.append(pv_id)

    async def _mint(*a: object, **k: object) -> None:
        mint_calls.append((a, k))

    monkeypatch.setattr(katzen, "QMessageBox", _StubMsgBox)
    monkeypatch.setattr(katzen, "QInputDialog", _InputDialog)
    monkeypatch.setattr(katzen, "conversation_is_joined", _not_joined)
    monkeypatch.setattr(katzen, "pending_voucher_for", _pending)
    monkeypatch.setattr(katzen, "_dialog_finished", _dialog_finished)
    monkeypatch.setattr(katzen, "cancel_pending_voucher", _cancel)
    monkeypatch.setattr(katzen, "mint_and_publish", _mint)
    return cancel_calls, mint_calls


async def _run_generate_voucher() -> None:
    run = katzen.MainWindow.generate_voucher.__get__(_fake_window())
    await run()  # async_cb returns the task; awaiting re-raises any failure


@pytest.mark.asyncio
async def test_replace_prompt_no_returns_without_cancelling(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel_calls, mint_calls = _install_stubs(
        monkeypatch, iter([_StubMsgBox.StandardButton.No]),
    )
    await _run_generate_voucher()
    assert cancel_calls == []
    assert mint_calls == []


@pytest.mark.asyncio
async def test_replace_prompt_yes_cancels_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel_calls, mint_calls = _install_stubs(
        monkeypatch,
        iter([_StubMsgBox.StandardButton.Yes, 0]),  # second result: name prompt dismissed
    )
    await _run_generate_voucher()
    assert len(cancel_calls) == 1
    assert mint_calls == []
