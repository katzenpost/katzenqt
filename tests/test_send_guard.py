"""Sending before membership must be refused.

A joiner's write stream is salt-mutated at induction, so a message committed
before then sits on a stream the group never reads; an owner has no audience
until they induct someone. The GUI send entry points guard on
``conversation_is_joined`` and leave the user's input in place. These tests pin
the guard's decision and its warning, using the same stubbed-loop pattern as
``test_listener_hardening`` (no real Qt objects are constructed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from katzenqt import katzen


class _Loop:
    """Stand-in for the io thread: runs the queued coroutine on this loop."""

    async def run_in_io(self, fn):
        if not asyncio.iscoroutine(fn):
            raise TypeError("A coroutine object is required")
        return await fn


def _fake_window() -> SimpleNamespace:
    return SimpleNamespace(iothread=_Loop())


@pytest.mark.asyncio
async def test_refuse_unless_joined_blocks_and_warns(monkeypatch):
    shown = []

    async def not_joined(_conversation_id):
        return False

    monkeypatch.setattr(katzen, "conversation_is_joined", not_joined)
    monkeypatch.setattr(
        katzen, "QTimer", SimpleNamespace(singleShot=lambda _ms, fn: fn()),
    )
    monkeypatch.setattr(
        katzen, "QMessageBox",
        SimpleNamespace(information=lambda *a, **k: shown.append(a)),
    )

    refuse = katzen.MainWindow._refuse_unless_joined.__get__(_fake_window())
    assert await refuse(42) is True
    assert shown, "expected a warning to be shown"


@pytest.mark.asyncio
async def test_refuse_unless_joined_allows_a_member(monkeypatch):
    shown = []

    async def joined(_conversation_id):
        return True

    monkeypatch.setattr(katzen, "conversation_is_joined", joined)
    monkeypatch.setattr(
        katzen, "QTimer", SimpleNamespace(singleShot=lambda _ms, fn: fn()),
    )
    monkeypatch.setattr(
        katzen, "QMessageBox",
        SimpleNamespace(information=lambda *a, **k: shown.append(a)),
    )

    refuse = katzen.MainWindow._refuse_unless_joined.__get__(_fake_window())
    assert await refuse(42) is False
    assert shown == []
