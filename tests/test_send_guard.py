"""Sending before membership must be refused.

A joiner's write stream is salt-mutated at induction, so a message committed
before then sits on a stream the group never reads; an owner has no audience
until they induct someone. The GUI send entry points guard on
``conversation_is_joined`` and leave the user's input in place. These tests pin
the guard's decision and its warning, and drive ``chat_msg_single_line`` and
``send_file`` through it, using the same stubbed-loop pattern as
``test_listener_hardening`` (no real Qt objects are constructed).
"""
from __future__ import annotations

import asyncio
import uuid
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


class _YieldingLoop(_Loop):
    """An io hop that really suspends, so a second trigger can interleave."""

    async def run_in_io(self, fn):
        await asyncio.sleep(0)
        return await super().run_in_io(fn)


class _Edit:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text


@pytest.fixture
def shown(monkeypatch):
    shown = []
    monkeypatch.setattr(
        katzen, "QTimer", SimpleNamespace(singleShot=lambda _ms, fn: fn()),
    )
    monkeypatch.setattr(
        katzen, "QMessageBox",
        SimpleNamespace(information=lambda *a, **k: shown.append(a)),
    )
    return shown


@pytest.fixture
def sent(monkeypatch):
    sent = []

    async def membership_hash_for(_conversation_id):
        return bytes(32)

    async def notify_outbound_chat_sent(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(
        katzen.conversation_handlers, "membership_hash_for", membership_hash_for,
    )
    monkeypatch.setattr(
        katzen.network, "notify_outbound_chat_sent", notify_outbound_chat_sent,
    )
    return sent


def _joined(monkeypatch, value: bool) -> None:
    async def conversation_is_joined(_conversation_id):
        return value

    monkeypatch.setattr(katzen, "conversation_is_joined", conversation_is_joined)


def _chat_window(text: str) -> SimpleNamespace:
    convo = SimpleNamespace(
        conversation_id=1, own_peer_id=1, own_peer_bacap_uuid=uuid.uuid4(),
        chat_lineEdit_buffer="", attached_files=set(),
    )
    window = SimpleNamespace(
        ui=SimpleNamespace(chat_lineEdit=_Edit(text)),
        iothread=_YieldingLoop(),
        convo=convo,
        convo_state=lambda: convo,
        convo_state_or_none=lambda: convo,
    )
    for name in ("_refuse_unless_joined", "_restore_unsent_text"):
        setattr(window, name, getattr(katzen.MainWindow, name).__get__(window))
    return window


@pytest.mark.asyncio
async def test_a_second_enter_does_not_resend_the_text(monkeypatch, shown, sent):
    _joined(monkeypatch, True)
    window = _chat_window("hello")

    await asyncio.gather(
        katzen.MainWindow.chat_msg_single_line(window),
        katzen.MainWindow.chat_msg_single_line(window),
    )

    assert len(sent) == 1
    assert window.ui.chat_lineEdit.text() == ""


@pytest.mark.asyncio
async def test_a_refused_send_gives_the_text_back(monkeypatch, shown, sent):
    _joined(monkeypatch, False)
    window = _chat_window("hello")

    await katzen.MainWindow.chat_msg_single_line(window)

    assert sent == []
    assert shown
    assert window.ui.chat_lineEdit.text() == "hello"


@pytest.mark.asyncio
async def test_a_refused_send_does_not_clobber_newer_typing(monkeypatch, shown, sent):
    _joined(monkeypatch, False)
    window = _chat_window("hello")
    task = katzen.MainWindow.chat_msg_single_line(window)
    await asyncio.sleep(0)
    window.ui.chat_lineEdit.setText("typed meanwhile")

    await task

    assert window.ui.chat_lineEdit.text() == "typed meanwhile"


@pytest.mark.asyncio
async def test_a_refused_send_keeps_a_draft_for_the_conversation_left(
    monkeypatch, shown, sent,
):
    _joined(monkeypatch, False)
    window = _chat_window("hello")
    window.convo_state_or_none = lambda: SimpleNamespace()

    await katzen.MainWindow.chat_msg_single_line(window)

    assert window.convo.chat_lineEdit_buffer == "hello"
    assert window.ui.chat_lineEdit.text() == ""


@pytest.mark.asyncio
async def test_a_refused_file_send_keeps_the_queued_attachments(
    monkeypatch, shown, sent,
):
    _joined(monkeypatch, False)
    window = _chat_window("")
    window.convo.attached_files = {"/tmp/a.txt"}

    async def enqueue_must_not_run(*_args, **_kwargs):
        raise AssertionError("a refused send reached the outbound writer")

    window._enqueue_outgoing_gcm = enqueue_must_not_run

    await katzen.MainWindow.send_file(window)

    assert window.convo.attached_files == {"/tmp/a.txt"}
    assert shown
    assert sent == []
