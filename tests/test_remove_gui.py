from collections.abc import Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar, cast

import asyncio
import uuid

import pytest
from PySide6.QtGui import QStandardItemModel
from sqlmodel import select

from katzenqt import katzen, persistent
from tests.test_membership_hash import _make_conversation

if TYPE_CHECKING:
    from katzenqt.qt_models import ConversationUIState

_T = TypeVar("_T")


def _as_window(window: SimpleNamespace) -> "katzen.MainWindow":
    return cast("katzen.MainWindow", window)


class _Loop:
    async def run_in_io(self, coro: "Coroutine[object, object, _T]") -> _T:
        return await coro


class _Model:
    def __init__(self) -> None:
        self.refreshed = 0

    def refresh_row_count(self) -> bool:
        self.refreshed += 1
        return True


class _Root:
    def __init__(self) -> None:
        self.props: "dict[str, object]" = {}

    def setProperty(self, name: str, value: object) -> None:
        self.props[name] = value


class _Panel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _conversation_item(conversation_id: int, name: str) -> "katzen.ContactsItem":
    item = katzen.ContactsItem(name)
    item.conversation_id = conversation_id
    return item


def _peer_item(name: str, read_cap_id: "uuid.UUID | None" = None) -> "katzen.ContactsItem":
    item = katzen.ContactsItem(name)
    if read_cap_id is not None:
        item.peer_read_cap_id = read_cap_id
    return item


class _Confirm:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: "list[str]" = []

    async def __call__(self, text: str) -> bool:
        self.asked.append(text)
        return self.answer


def _window(
    model: QStandardItemModel,
    states: "dict[int, SimpleNamespace]",
    current: "SimpleNamespace | None" = None,
    answer: bool = True,
) -> SimpleNamespace:
    confirm = _Confirm(answer)
    root = _Root()
    window = SimpleNamespace(
        all_contacts=model,
        conversation_state_by_id=states,
        _poll_windows={},
        _voucher_join_tasks={},
        iothread=_Loop(),
        settings={},
        cleared=0,
        failures=[],
        _confirm=confirm,
        convo_state_or_none=lambda: current,
        ui=SimpleNamespace(
            qml_ChatLines=SimpleNamespace(rootObject=lambda: root)
        ),
        root=root,
    )
    window._clear_chat_view = lambda: setattr(
        window, "cleared", window.cleared + 1
    )
    window._report_removal_failure = window.failures.append
    return window


def _state(log_model: _Model) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_log_model=log_model,
        qml_ctx=lambda root, settings: "ctx",
    )


def test_drop_peer_ui_removes_the_row_and_refreshes_the_chat() -> None:
    model = QStandardItemModel()
    conv = _conversation_item(1, "demo")
    alice, bob = _peer_item("alice"), _peer_item("bob")
    conv.appendRow(alice)
    conv.appendRow(bob)
    model.appendRow(conv)
    log_model = _Model()
    state = _state(log_model)
    window = _window(model, {1: state}, current=state)

    katzen.MainWindow._drop_peer_ui(_as_window(window), conv, alice)

    assert [conv.child(r).text() for r in range(conv.rowCount())] == ["bob"]
    assert log_model.refreshed == 1
    assert window.root.props == {"ctx": "ctx"}


def test_drop_peer_ui_leaves_an_unselected_chat_view_alone() -> None:
    model = QStandardItemModel()
    conv = _conversation_item(1, "demo")
    alice = _peer_item("alice")
    conv.appendRow(alice)
    model.appendRow(conv)
    log_model = _Model()
    window = _window(model, {1: _state(log_model)}, current=None)

    katzen.MainWindow._drop_peer_ui(_as_window(window), conv, alice)

    assert log_model.refreshed == 1
    assert window.root.props == {}


def test_drop_conversation_ui_forgets_state_and_closes_its_polls() -> None:
    model = QStandardItemModel()
    gone, kept = _conversation_item(1, "gone"), _conversation_item(2, "kept")
    model.appendRow(gone)
    model.appendRow(kept)
    states = {1: _state(_Model()), 2: _state(_Model())}
    window = _window(model, states, current=states[2])
    mine, theirs = _Panel(), _Panel()
    window._poll_windows = {(1, b"a"): mine, (2, b"b"): theirs}

    katzen.MainWindow._drop_conversation_ui(_as_window(window), gone)

    assert model.rowCount() == 1 and model.item(0).text() == "kept"
    assert list(states) == [2]
    assert mine.closed and not theirs.closed
    assert window.cleared == 0


@pytest.mark.asyncio
async def test_drop_conversation_ui_cancels_its_voucher_join() -> None:
    model = QStandardItemModel()
    gone = _conversation_item(1, "gone")
    model.appendRow(gone)
    window = _window(model, {1: _state(_Model())}, current=None)
    task = asyncio.create_task(asyncio.Event().wait())
    window._voucher_join_tasks = {1: task}

    katzen.MainWindow._drop_conversation_ui(_as_window(window), gone)
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert window._voucher_join_tasks == {}


def test_drop_last_conversation_clears_the_chat_view() -> None:
    model = QStandardItemModel()
    only = _conversation_item(1, "only")
    model.appendRow(only)
    window = _window(model, {1: _state(_Model())}, current=None)

    katzen.MainWindow._drop_conversation_ui(_as_window(window), only)

    assert model.rowCount() == 0
    assert window.cleared == 1


async def _wired_peer(
    answer: bool,
) -> "tuple[SimpleNamespace, katzen.ContactsItem, katzen.ContactsItem, int]":
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        assert conv is not None
        alice = next(p for p in conv.peers if p.name == "alice")
        read_cap_id = alice.read_cap_id
    model = QStandardItemModel()
    conv_item = _conversation_item(conv_id, "demo")
    item = _peer_item("alice", read_cap_id)
    conv_item.appendRow(item)
    model.appendRow(conv_item)
    state = _state(_Model())
    window = _window(model, {conv_id: state}, current=state, answer=answer)

    def drop_peer_ui(
        conversation_item: "katzen.ContactsItem", peer: "katzen.ContactsItem",
    ) -> None:
        katzen.MainWindow._drop_peer_ui(
            _as_window(window), conversation_item, peer,
        )

    window._drop_peer_ui = drop_peer_ui
    return window, conv_item, item, conv_id


@pytest.mark.asyncio
async def test_remove_peer_confirmed_deletes_state_and_the_row() -> None:
    window, conv_item, item, conv_id = await _wired_peer(answer=True)

    await katzen.MainWindow._remove_peer(_as_window(window), item)

    assert conv_item.rowCount() == 0
    async with persistent.asession() as sess:
        names = [
            p.name
            for p in (
                await sess.exec(select(persistent.ConversationPeer))
            ).all()
        ]
    assert names == ["me"]
    assert (
        "alice" in window._confirm.asked[0]
        and "demo" in window._confirm.asked[0]
    )


@pytest.mark.asyncio
async def test_remove_peer_declined_changes_nothing() -> None:
    window, conv_item, item, _ = await _wired_peer(answer=False)

    await katzen.MainWindow._remove_peer(_as_window(window), item)

    assert conv_item.rowCount() == 1
    async with persistent.asession() as sess:
        assert (
            len((await sess.exec(select(persistent.ConversationPeer))).all())
            == 2
        )


@pytest.mark.asyncio
async def test_remove_conversation_confirmed_deletes_it_and_drops_the_row() -> None:
    conv_id = await _make_conversation()
    model = QStandardItemModel()
    item = _conversation_item(conv_id, "demo")
    model.appendRow(item)
    window = _window(model, {conv_id: _state(_Model())}, current=None)

    def drop_conversation_ui(conversation_item: "katzen.ContactsItem") -> None:
        katzen.MainWindow._drop_conversation_ui(
            _as_window(window), conversation_item,
        )

    window._drop_conversation_ui = drop_conversation_ui

    await katzen.MainWindow._remove_conversation(_as_window(window), item)

    assert model.rowCount() == 0 and window.conversation_state_by_id == {}
    async with persistent.asession() as sess:
        assert await sess.get(persistent.Conversation, conv_id) is None


@pytest.mark.asyncio
async def test_remove_conversation_failure_is_reported_and_the_row_kept() -> None:
    model = QStandardItemModel()
    item = _conversation_item(424242, "ghost")
    model.appendRow(item)
    window = _window(model, {424242: _state(_Model())}, current=None)

    await katzen.MainWindow._remove_conversation(_as_window(window), item)

    assert model.rowCount() == 1
    assert len(window.failures) == 1


@pytest.mark.asyncio
async def test_voucher_join_is_tracked_while_it_runs() -> None:
    started = asyncio.Event()

    async def run(convo: object) -> None:
        started.set()
        await asyncio.Event().wait()

    window = SimpleNamespace(_voucher_join_tasks={}, _run_voucher_join=run)
    task = asyncio.create_task(
        katzen.MainWindow._await_voucher_join(
            _as_window(window),
            cast("ConversationUIState", SimpleNamespace(conversation_id=7)),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert window._voucher_join_tasks == {7: task}

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert window._voucher_join_tasks == {}
