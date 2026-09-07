"""The two UI listeners must survive a per-item error (log-and-continue).

``receive_msg_listener`` and ``peer_added_listener`` are bare ``while True:``
loops; any unexpected exception used to kill them until a full restart,
silently freezing UI refresh. They now wrap the whole per-item unit
(queue-get through UI refresh) in try/except, re-raising only
``CancelledError``. Stub-based tests pin that a bad item is logged and the
loop keeps serving the next one.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from katzenqt import katzen, network


class _BlockedLoop:
    """Stand-in for the io thread: actually runs the queued coroutine on
    this loop, as ``MainWindow.run_in_io`` does over the thread hop."""

    async def run_in_io(self, fn):
        if asyncio.iscoroutine(fn):
            # the real caller passes network...queue.get(), a coroutine,
            # straight to run_coroutine_threadsafe; the get() body only
            # runs on await.
            return await fn
        return await fn()


class _FakeQueue:
    """One-shot queue: yields the canned items, then raises
    CancelledError, which the listener re-raises so the test task ends."""

    def __init__(self, items):
        self.items = list(items)

    async def get(self):
        try:
            return self.items.pop(0)
        except IndexError:
            raise asyncio.CancelledError


class _BoomLogModel:
    def __init__(self):
        self.calls = []

    def increment_row_count(self):
        self.calls.append("increment")
        raise RuntimeError("boom")

    def redraw_network_status(self):
        self.calls.append("redraw")


class _ConvoState:
    def __init__(self, log_model):
        self.conversation_log_model = log_model
        self.chat_lines_scroll_idx = 0.0


async def _run_until_cancelled(listener_coro):
    task = asyncio.create_task(listener_coro)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)


async def _state_ready(*_a, **_k):
    return True


def _fake_window(**overrides):
    base = dict(
        iothread=_BlockedLoop(),
        _wait_for_conversation_state=_state_ready,
        conversation_state_by_id={},
        convo_state_or_none=lambda: None,
        app=SimpleNamespace(focusWidget=lambda: True),
        systray=SimpleNamespace(has_new_messages=lambda: None),
    )
    base.update(overrides)
    window = SimpleNamespace(**base)
    # The loop bodies call these back through `self`, which a bare
    # SimpleNamespace cannot resolve from the class.
    window._process_conversation_update = (
        katzen.MainWindow._process_conversation_update.__get__(window)
    )
    window._process_peer_added = (
        katzen.MainWindow._process_peer_added.__get__(window)
    )
    return window


class TestReceiveMsgListenerSurvives:
    @pytest.mark.asyncio
    async def test_bad_item_is_logged_and_next_item_still_served(
        self, monkeypatch, caplog,
    ):
        # must be present before _process_conversation_update runs
        log_model = _BoomLogModel()
        conversation_id = 1
        queue = _FakeQueue([
            (conversation_id, False),  # increment_row_count raises
            (conversation_id, True),   # redraw path must still work
        ])
        monkeypatch.setattr(network, "conversation_update_queue", queue)

        window = _fake_window(conversation_state_by_id={
            conversation_id: _ConvoState(log_model),
        })
        with caplog.at_level(logging.ERROR, logger="katzen"):
            await _run_until_cancelled(
                katzen.MainWindow.receive_msg_listener.__get__(window)()
            )

        assert log_model.calls == ["increment", "redraw"]
        assert any(
            "receive_msg_listener: dropping an item after boom" in r.message
            for r in caplog.records
        )


class TestPeerAddedListenerSurvives:
    @pytest.mark.asyncio
    async def test_bad_item_is_logged_and_next_item_still_served(
        self, monkeypatch, caplog,
    ):
        class _ContactsItem:
            def __init__(self):
                self.rows = []
                self.append_calls = 0

            def rowCount(self):
                return len(self.rows)

            def child(self, r):
                return SimpleNamespace(text=lambda: f"peer-{r}")

            def appendRow(self, item):
                self.append_calls += 1
                if item == "BOOM":
                    raise RuntimeError("boom")
                self.rows.append(item)

        class _State:
            contacts_standard_item = _ContactsItem()

        fake_item_class = lambda name: name  # noqa: E731 - stand-in for QStandardItem

        conversation_id = 1
        queue = _FakeQueue([
            (conversation_id, "BOOM"),  # appendRow raises
            (conversation_id, "alice"),  # must still be added
        ])
        monkeypatch.setattr(network, "peer_added_queue", queue)
        monkeypatch.setattr(katzen, "QStandardItem", fake_item_class)

        window = _fake_window(conversation_state_by_id={
            conversation_id: _State(),
        })
        with caplog.at_level(logging.ERROR, logger="katzen"):
            await _run_until_cancelled(
                katzen.MainWindow.peer_added_listener.__get__(window)()
            )

        item = _State.contacts_standard_item
        assert item.append_calls == 2
        assert item.rows == ["alice"]
        assert any(
            "peer_added_listener: dropping an item after boom" in r.message
            for r in caplog.records
        )


class TestPeerAddedDedups:
    @pytest.mark.asyncio
    async def test_same_name_is_not_appended_twice(self):
        class _ContactsItem:
            def __init__(self):
                self.rows = []

            def rowCount(self):
                return len(self.rows)

            def child(self, r):
                # child(r) mirrors a tree where every existing row is "bob"
                return SimpleNamespace(text=lambda: "bob")

            def appendRow(self, item):
                self.rows.append(item)

        class _State:
            contacts_standard_item = _ContactsItem()

        conversation_id = 1
        window = _fake_window(conversation_state_by_id={
            conversation_id: _State(),
        })
        process = katzen.MainWindow._process_peer_added.__get__(window)
        await process(conversation_id, "bob")
        await process(conversation_id, "bob")
        # appended once then deduplicated by name
        assert len(_State.contacts_standard_item.rows) == 1