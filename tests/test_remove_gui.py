from types import SimpleNamespace

import pytest
from PySide6.QtGui import QStandardItem, QStandardItemModel
from sqlmodel import select

from katzenqt import katzen, persistent
from tests.test_membership_hash import _make_conversation


class _Loop:
    async def run_in_io(self, coro):
        return await coro


class _Model:
    def __init__(self) -> None:
        self.refreshed = 0

    def refresh_row_count(self) -> bool:
        self.refreshed += 1
        return True


class _Root:
    def __init__(self) -> None:
        self.props: dict = {}

    def setProperty(self, name, value) -> None:
        self.props[name] = value


class _Panel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _conversation_item(conversation_id: int, name: str) -> QStandardItem:
    item = QStandardItem(name)
    item.conversation_id = conversation_id
    return item


def _window(model: QStandardItemModel, states: dict, current=None, answer=True) -> SimpleNamespace:
    async def confirm(text: str) -> bool:
        confirm.asked.append(text)
        return answer

    confirm.asked = []
    window = SimpleNamespace(
        all_contacts=model,
        conversation_state_by_id=states,
        _poll_windows={},
        iothread=_Loop(),
        settings={},
        cleared=0,
        failures=[],
        _confirm=confirm,
        convo_state_or_none=lambda: current,
        ui=SimpleNamespace(qml_ChatLines=SimpleNamespace(rootObject=lambda: window.root)),
        root=_Root(),
    )
    window._clear_chat_view = lambda: setattr(window, "cleared", window.cleared + 1)
    window._report_removal_failure = window.failures.append
    return window


def _state(log_model: _Model) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_log_model=log_model,
        qml_ctx=lambda root, settings: "ctx",
    )


def test_drop_peer_ui_removes_the_row_and_refreshes_the_chat():
    model = QStandardItemModel()
    conv = _conversation_item(1, "demo")
    alice, bob = QStandardItem("alice"), QStandardItem("bob")
    conv.appendRow(alice)
    conv.appendRow(bob)
    model.appendRow(conv)
    log_model = _Model()
    state = _state(log_model)
    window = _window(model, {1: state}, current=state)

    katzen.MainWindow._drop_peer_ui(window, conv, alice)

    assert [conv.child(r).text() for r in range(conv.rowCount())] == ["bob"]
    assert log_model.refreshed == 1
    assert window.root.props == {"ctx": "ctx"}


def test_drop_peer_ui_leaves_an_unselected_chat_view_alone():
    model = QStandardItemModel()
    conv = _conversation_item(1, "demo")
    alice = QStandardItem("alice")
    conv.appendRow(alice)
    model.appendRow(conv)
    log_model = _Model()
    window = _window(model, {1: _state(log_model)}, current=None)

    katzen.MainWindow._drop_peer_ui(window, conv, alice)

    assert log_model.refreshed == 1
    assert window.root.props == {}


def test_drop_conversation_ui_forgets_state_and_closes_its_polls():
    model = QStandardItemModel()
    gone, kept = _conversation_item(1, "gone"), _conversation_item(2, "kept")
    model.appendRow(gone)
    model.appendRow(kept)
    states = {1: _state(_Model()), 2: _state(_Model())}
    window = _window(model, states, current=states[2])
    mine, theirs = _Panel(), _Panel()
    window._poll_windows = {(1, b"a"): mine, (2, b"b"): theirs}

    katzen.MainWindow._drop_conversation_ui(window, gone)

    assert model.rowCount() == 1 and model.item(0).text() == "kept"
    assert list(states) == [2]
    assert mine.closed and not theirs.closed
    assert window.cleared == 0


def test_drop_last_conversation_clears_the_chat_view():
    model = QStandardItemModel()
    only = _conversation_item(1, "only")
    model.appendRow(only)
    window = _window(model, {1: _state(_Model())}, current=None)

    katzen.MainWindow._drop_conversation_ui(window, only)

    assert model.rowCount() == 0
    assert window.cleared == 1


async def _wired_peer(answer: bool):
    conv_id = await _make_conversation()
    async with persistent.asession() as sess:
        conv = await sess.get(persistent.Conversation, conv_id)
        alice = next(p for p in conv.peers if p.name == "alice")
        read_cap_id = alice.read_cap_id
    model = QStandardItemModel()
    conv_item = _conversation_item(conv_id, "demo")
    item = QStandardItem("alice")
    item.peer_read_cap_id = read_cap_id
    conv_item.appendRow(item)
    model.appendRow(conv_item)
    state = _state(_Model())
    window = _window(model, {conv_id: state}, current=state, answer=answer)
    window._drop_peer_ui = lambda *a: katzen.MainWindow._drop_peer_ui(window, *a)
    window._peer_id_of = lambda it: katzen.MainWindow._peer_id_of(window, it)
    return window, conv_item, item, conv_id


@pytest.mark.asyncio
async def test_remove_peer_confirmed_deletes_state_and_the_row():
    window, conv_item, item, conv_id = await _wired_peer(answer=True)

    await katzen.MainWindow._remove_peer(window, item)

    assert conv_item.rowCount() == 0
    async with persistent.asession() as sess:
        names = [p.name for p in (await sess.exec(select(persistent.ConversationPeer))).all()]
    assert names == ["me"]
    assert "alice" in window._confirm.asked[0] and "demo" in window._confirm.asked[0]


@pytest.mark.asyncio
async def test_remove_peer_declined_changes_nothing():
    window, conv_item, item, _ = await _wired_peer(answer=False)

    await katzen.MainWindow._remove_peer(window, item)

    assert conv_item.rowCount() == 1
    async with persistent.asession() as sess:
        assert len((await sess.exec(select(persistent.ConversationPeer))).all()) == 2


@pytest.mark.asyncio
async def test_remove_conversation_confirmed_deletes_it_and_drops_the_row():
    conv_id = await _make_conversation()
    model = QStandardItemModel()
    item = _conversation_item(conv_id, "demo")
    model.appendRow(item)
    window = _window(model, {conv_id: _state(_Model())}, current=None)
    window._drop_conversation_ui = lambda it: katzen.MainWindow._drop_conversation_ui(window, it)

    await katzen.MainWindow._remove_conversation(window, item)

    assert model.rowCount() == 0 and window.conversation_state_by_id == {}
    async with persistent.asession() as sess:
        assert await sess.get(persistent.Conversation, conv_id) is None


@pytest.mark.asyncio
async def test_remove_conversation_failure_is_reported_and_the_row_kept():
    model = QStandardItemModel()
    item = _conversation_item(424242, "ghost")
    model.appendRow(item)
    window = _window(model, {424242: _state(_Model())}, current=None)

    await katzen.MainWindow._remove_conversation(window, item)

    assert model.rowCount() == 1
    assert len(window.failures) == 1
