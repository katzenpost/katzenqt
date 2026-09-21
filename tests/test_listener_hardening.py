"""The UI listeners must survive a per-item error (log-and-continue).

``receive_msg_listener``, ``peer_added_listener``, and ``transfers_listener``
are bare ``while True:`` loops; each wraps the whole per-item unit
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
        if not asyncio.iscoroutine(fn):
            # mirror asyncio.run_coroutine_threadsafe's contract: callers
            # pass a started coroutine (network...queue.get(), not .get),
            # and a bare callable must fail loudly rather than be tolerated.
            raise TypeError("A coroutine object is required")
        return await fn


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

    def refresh_row_count(self):
        self.calls.append("refresh")
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

        assert log_model.calls == ["refresh", "redraw"]
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


class TestTransfersListenerDrainsEvents:
    """The Transfers listener turns each substream_progress_queue
    event into a DownloadsModel call, and survives a per-item error via
    log-and-continue like the other UI listeners."""

    def _fake_transfers_model(self, boom_on="started"):
        class _Model:
            def __init__(self):
                self.calls = []

            def start_transfer(self, rcw_id, conv_id, parent_name, total,
                               direction="download", raw_bytes=0):
                self.calls.append(
                    ("start", rcw_id, conv_id, parent_name, total, direction,
                     raw_bytes),
                )
                if boom_on == "started":
                    raise RuntimeError("boom")

            def notify_piece(self, rcw_id, pieces, raw_bytes=None):
                self.calls.append(("piece", rcw_id, pieces, raw_bytes))
                if boom_on == "piece":
                    raise RuntimeError("boom")

            def complete_transfer(self, rcw_id):
                self.calls.append(("complete", rcw_id))

            def set_paused(self, rcw_id, paused):
                self.calls.append(("paused", rcw_id, paused))

        return _Model()

    @pytest.mark.asyncio
    async def test_events_are_dispatched_to_the_model(self, monkeypatch):
        rcw = __import__("uuid").uuid4()
        up_rcw = __import__("uuid").uuid4()
        queue = _FakeQueue([
            ("started", rcw, 7, 3, "alice"),
            ("piece", rcw, 1, 1529),
            ("piece", rcw, 2, 3058),
            ("paused", rcw),
            ("resumed", rcw),
            ("completed", rcw),
            ("upload_started", up_rcw, 7, 25, 25000, "bob-conv"),
            ("upload_piece", up_rcw, 6, 30000),
            ("upload_paused", up_rcw),
            ("upload_resumed", up_rcw),
            ("upload_completed", up_rcw),
        ])
        monkeypatch.setattr(network, "substream_progress_queue", queue)
        model = self._fake_transfers_model(boom_on=None)
        window = _fake_window(transfers_model=model)

        await _run_until_cancelled(
            katzen.MainWindow.transfers_listener.__get__(window)()
        )

        assert model.calls == [
            ("start", rcw, 7, "alice", 3, "download", 0),
            ("piece", rcw, 1, 1529),
            ("piece", rcw, 2, 3058),
            ("paused", rcw, True),
            ("paused", rcw, False),  # resumed event -> set_paused(paused=False)
            ("complete", rcw),
            ("start", up_rcw, 7, "bob-conv", 25, "upload", 25000),
            ("piece", up_rcw, 6, 30000),
            ("paused", up_rcw, True),
            ("paused", up_rcw, False),
            ("complete", up_rcw),
        ]

    @pytest.mark.asyncio
    async def test_bad_item_is_logged_and_next_item_still_served(
        self, monkeypatch, caplog,
    ):
        rcw = __import__("uuid").uuid4()
        queue = _FakeQueue([
            ("started", rcw, 7, 3, "alice"),  # boom
            ("piece", rcw, 1, 1529),          # must still get through
        ])
        monkeypatch.setattr(network, "substream_progress_queue", queue)
        window = _fake_window(
            transfers_model=self._fake_transfers_model(boom_on="started"),
        )
        with caplog.at_level(logging.ERROR, logger="katzen"):
            await _run_until_cancelled(
                katzen.MainWindow.transfers_listener.__get__(window)()
            )

        assert window.transfers_model.calls == [
            ("start", rcw, 7, "alice", 3, "download", 0),
            ("piece", rcw, 1, 1529),
        ]
        assert any(
            "transfers_listener: dropping an item after boom" in r.message
            for r in caplog.records
        )