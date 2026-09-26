from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable, Coroutine

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import katzen, network, persistent  # noqa: E402
from katzenqt.tally import presenter as tally_presenter  # noqa: E402
from katzenqt.tally import schema as tally_schema  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    FakeMessageBox,
    add_seeded_conversation,
    boxes,
    fresh_queues,
    instant_timer,
    loaded_window,
    qt_app,
    seed_conversation,
    window,
)


class StepClock:
    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def monotonic(self) -> float:
        return self._values.pop(0) if self._values else 5.0

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@pytest.fixture(autouse=True)
def _rebind_queues(fresh_queues: None) -> None:
    return None


async def run_briefly(
    coroutine_factory: Callable[[], Coroutine[object, object, None]],
    rounds: int = 40,
) -> None:
    task = asyncio.ensure_future(coroutine_factory())
    for _ in range(rounds):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_waiting_for_a_known_conversation_returns_at_once(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    assert (
        await loaded_window._wait_for_conversation_state(
            convo_id,
            what="test",
        )
        is True
    )


@pytest.mark.asyncio
async def test_waiting_for_an_unknown_conversation_gives_up(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    assert (
        await window._wait_for_conversation_state(
            4242,
            what="receive_msg_listener",
        )
        is False
    )
    assert "conversation_id 4242 never appeared" in caplog.text


@pytest.mark.asyncio
async def test_the_receive_listener_refreshes_the_focused_conversation(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    await network.conversation_update_queue.put((convo_id, False))
    await run_briefly(loaded_window.receive_msg_listener)
    assert loaded_window.convo_state().chat_lines_scroll_idx == 1.0


@pytest.mark.asyncio
async def test_a_redraw_only_update_only_restyles_the_rows(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    before = loaded_window.systray.new_messages
    await loaded_window._process_conversation_update(convo_id, True)
    assert loaded_window.systray.new_messages == before


@pytest.mark.asyncio
async def test_an_update_for_a_background_conversation_bumps_the_scroll(
    loaded_window: katzen.MainWindow,
) -> None:
    other = await seed_conversation(name="background room", own_name="me2")
    await add_seeded_conversation(loaded_window, other.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    first = next(
        state
        for cid, state in loaded_window.conversation_state_by_id.items()
        if cid != other.conversation_id
    )
    before = first.chat_lines_scroll_idx
    await loaded_window._process_conversation_update(
        first.conversation_id,
        False,
    )
    assert first.chat_lines_scroll_idx == before + 1.0
    assert loaded_window.systray.new_messages == 1


@pytest.mark.asyncio
async def test_the_receive_listener_survives_a_bad_queue_item(
    loaded_window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    await network.conversation_update_queue.put("not a pair")
    await run_briefly(loaded_window.receive_msg_listener)
    assert "receive_msg_listener: dropping an item after" in caplog.text


@pytest.mark.asyncio
async def test_the_peer_listener_adds_an_announced_member(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    await network.peer_added_queue.put((convo_id, "frank"))
    await run_briefly(loaded_window.peer_added_listener)
    item = loaded_window.convo_state().contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert "frank" in names


@pytest.mark.asyncio
async def test_an_announced_member_is_added_only_once(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    await loaded_window._process_peer_added(convo_id, "gina")
    await loaded_window._process_peer_added(convo_id, "gina")
    item = loaded_window.convo_state().contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert names.count("gina") == 1


@pytest.mark.asyncio
async def test_an_announced_peer_carries_its_read_cap_tag(
    window: katzen.MainWindow,
) -> None:
    seeded = await seed_conversation(peers=("hank",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    state = window.conversation_state_by_id[seeded.conversation_id]
    state.contacts_standard_item.removeRows(
        0,
        state.contacts_standard_item.rowCount(),
    )
    await window._process_peer_added(seeded.conversation_id, "hank")
    added = state.contacts_standard_item.child(0)
    assert added.text() == "hank"
    assert added.peer_is_own is False
    assert isinstance(added.peer_read_cap_id, uuid.UUID)


@pytest.mark.asyncio
async def test_a_substream_peer_is_never_rendered(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    item = loaded_window.convo_state().contacts_standard_item
    before = item.rowCount()
    await loaded_window._process_peer_added(
        convo_id,
        f"{network._SUBSTREAM_NAME_PREFIX}parent:aa",
    )
    assert item.rowCount() == before


@pytest.mark.asyncio
async def test_the_peer_listener_survives_a_bad_queue_item(
    loaded_window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    await network.peer_added_queue.put(None)
    await run_briefly(loaded_window.peer_added_listener)
    assert "peer_added_listener: dropping an item after" in caplog.text


@pytest.mark.asyncio
async def test_the_transfers_listener_tracks_a_download(
    window: katzen.MainWindow,
) -> None:
    stream = uuid.uuid4()
    for event in (
        ("started", stream, 1, 3, "bob"),
        ("piece", stream, 2, 3),
        ("paused", stream),
        ("resumed", stream),
    ):
        network.substream_progress_queue.put_nowait(event)
    await run_briefly(window.transfers_listener)
    row = window.transfers_model._rows[stream]
    assert row["parent_name"] == "bob"
    assert row["pieces"] == 2
    assert row["total"] == 3
    assert row["active"] is True

    network.substream_progress_queue.put_nowait(("completed", stream))
    await run_briefly(window.transfers_listener)
    assert window.transfers_model.rowCount() == 0


@pytest.mark.asyncio
async def test_the_transfers_listener_tracks_an_upload(
    window: katzen.MainWindow,
) -> None:
    stream = uuid.uuid4()
    for event in (
        ("upload_started", stream, 1, 2, 4096, "bob", "pic.png"),
        ("upload_piece", stream, 1, 2),
        ("upload_paused", stream),
    ):
        network.substream_progress_queue.put_nowait(event)
    await run_briefly(window.transfers_listener)
    row = window.transfers_model._rows[stream]
    assert row["direction"] == "upload"
    assert row["parent_name"] == "pic.png (in bob)"
    assert row["active"] is False

    network.substream_progress_queue.put_nowait(("upload_resumed", stream))
    await run_briefly(window.transfers_listener)
    assert window.transfers_model._rows[stream]["active"] is True

    network.substream_progress_queue.put_nowait(("upload_cancelled", stream))
    await run_briefly(window.transfers_listener)
    assert window.transfers_model.rowCount() == 0


@pytest.mark.asyncio
async def test_an_upload_without_a_basename_uses_the_parent_name(
    window: katzen.MainWindow,
) -> None:
    stream = uuid.uuid4()
    network.substream_progress_queue.put_nowait(
        ("upload_started", stream, 1, 2, 4096, "bob", ""),
    )
    await run_briefly(window.transfers_listener)
    assert window.transfers_model._rows[stream]["parent_name"] == "bob"

    network.substream_progress_queue.put_nowait(("upload_completed", stream))
    await run_briefly(window.transfers_listener)
    assert window.transfers_model.rowCount() == 0


@pytest.mark.asyncio
async def test_a_failed_transfer_keeps_its_reason(
    window: katzen.MainWindow,
) -> None:
    stream = uuid.uuid4()
    network.substream_progress_queue.put_nowait(
        ("started", stream, 1, 3, "bob"),
    )
    network.substream_progress_queue.put_nowait(
        ("failed", stream, "missing box"),
    )
    await run_briefly(window.transfers_listener)
    assert window.transfers_model._rows[stream]["failed"] is True


@pytest.mark.asyncio
async def test_the_transfers_listener_survives_a_bad_event(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    network.substream_progress_queue.put_nowait(("started",))
    await run_briefly(window.transfers_listener)
    assert "transfers_listener: dropping an item after" in caplog.text


@pytest.mark.asyncio
async def test_the_tally_listener_refreshes_the_poll_views(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    await network.tally_update_queue.put(convo_id)
    await run_briefly(loaded_window.tally_listener)
    assert network.tally_update_queue.empty() is True


@pytest.mark.asyncio
async def test_the_tally_listener_survives_a_bad_item(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    await network.tally_update_queue.put(["unhashable"])
    await run_briefly(window.tally_listener)
    assert "tally_listener: dropping an item after" in caplog.text


@pytest.mark.asyncio
async def test_creating_a_survey_stages_it_for_broadcast(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Lunch?",
        tally_schema.Mode.AVAILABILITY,
        ["pizza", "soup"],
    )
    assert isinstance(survey_id, bytes)
    assert len(survey_id) == 16
    assert (
        await katzen._io_tally_vote(
            convo_id,
            survey_id,
            {"s0": "yes"},
        )
        is True
    )
    assert await katzen._io_tally_close(convo_id, survey_id) is True


@pytest.mark.asyncio
async def test_tally_operations_refuse_an_unknown_conversation(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="katzen")
    assert (
        await katzen._io_tally_create(
            4242, "t", tally_schema.Mode.APPROVAL, ["a"]
        )
        is None
    )
    assert await katzen._io_tally_vote(4242, b"s" * 16, {}) is False
    assert await katzen._io_tally_close(4242, b"s" * 16) is False
    assert "tally create: conversation 4242 not found" in caplog.text


@pytest.mark.asyncio
async def test_voting_on_an_unknown_survey_is_refused(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    assert await katzen._io_tally_vote(convo_id, b"z" * 16, {}) is False
    assert await katzen._io_tally_close(convo_id, b"z" * 16) is False


@pytest.mark.asyncio
async def test_a_slow_log_lock_is_reported(
    loaded_window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(katzen, "time", StepClock([0.0, 5.0]))
    caplog.set_level(logging.WARNING, logger="katzen")
    convo_id = loaded_window.convo_state().conversation_id
    await katzen._io_tally_create(
        convo_id,
        "Slow?",
        tally_schema.Mode.APPROVAL,
        ["a"],
    )
    assert "waited 5.0s for the conversation log lock" in caplog.text


@pytest.mark.asyncio
async def test_opening_a_poll_window_shows_the_survey(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Colour?",
        tally_schema.Mode.APPROVAL,
        ["red", "blue"],
    )
    assert survey_id is not None
    loaded_window.openPoll(survey_id.hex())
    key = (convo_id, survey_id)
    panel = loaded_window._poll_windows[key]
    assert panel.current_survey() == key
    loaded_window.openPoll(survey_id.hex())
    assert loaded_window._poll_windows[key] is panel
    loaded_window._refresh_tally_views(convo_id)
    assert panel.current_survey() == key
    panel.close()


@pytest.mark.asyncio
async def test_an_unknown_survey_leaves_no_poll_window(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    loaded_window._open_poll_window(convo_id, b"n" * 16)
    assert loaded_window._poll_windows == {}


@pytest.mark.asyncio
async def test_a_malformed_survey_id_opens_nothing(
    loaded_window: katzen.MainWindow,
) -> None:
    loaded_window.openPoll("not-hex")
    assert loaded_window._poll_windows == {}


def test_opening_a_poll_without_a_conversation_does_nothing(
    window: katzen.MainWindow,
) -> None:
    window.openPoll("00" * 16)
    assert window._poll_windows == {}


def test_refreshing_an_unknown_conversation_is_harmless(
    window: katzen.MainWindow,
) -> None:
    window._refresh_tally_views(4242)
    assert window._poll_windows == {}


@pytest.mark.asyncio
async def test_a_new_poll_before_joining_is_refused(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_joined(conversation_id: int) -> bool:
        return False

    monkeypatch.setattr(katzen, "conversation_is_joined", not_joined)
    await loaded_window.new_poll()
    assert boxes.seen[0].text.startswith(
        "You have not joined this conversation yet.",
    )


@pytest.mark.asyncio
async def test_a_new_poll_without_a_conversation_does_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[object] = []

    class _RecordingDialog:
        def __init__(self, parent: object) -> None:
            opened.append(parent)

    monkeypatch.setattr(katzen, "TallyCreateDialog", _RecordingDialog)
    assert window.convo_state_or_none() is None
    await window.new_poll()
    assert opened == []


@pytest.mark.asyncio
async def test_the_create_dialog_drives_the_poll_create(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def joined(conversation_id: int) -> bool:
        return True

    monkeypatch.setattr(katzen, "conversation_is_joined", joined)
    await loaded_window.new_poll()
    for _ in range(20):
        await asyncio.sleep(0)
    convo_id = loaded_window.convo_state().conversation_id
    await loaded_window._create_poll(
        convo_id,
        "Snack?",
        tally_schema.Mode.APPROVAL,
        ["nuts"],
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(loaded_window._poll_windows) == 1
    for panel in list(loaded_window._poll_windows.values()):
        panel.close()


@pytest.mark.asyncio
async def test_an_empty_poll_is_not_created(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    await loaded_window._create_poll(
        convo_id,
        "",
        tally_schema.Mode.APPROVAL,
        [],
    )
    assert loaded_window._poll_windows == {}


@pytest.mark.asyncio
async def test_a_failing_poll_create_is_reported(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no room on the stream")

    monkeypatch.setattr(katzen, "_io_tally_create", boom)
    convo_id = loaded_window.convo_state().conversation_id
    await loaded_window._create_poll(
        convo_id,
        "Topic",
        tally_schema.Mode.APPROVAL,
        ["a"],
    )
    assert boxes.seen[0].text == (
        "Could not create the poll:\nno room on the stream"
    )


@pytest.mark.asyncio
async def test_a_vote_is_staged_and_the_views_refresh(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def joined(conversation_id: int) -> bool:
        return True

    monkeypatch.setattr(katzen, "conversation_is_joined", joined)
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Tea?",
        tally_schema.Mode.APPROVAL,
        ["yes", "no"],
    )
    assert survey_id is not None
    loaded_window.openPoll(survey_id.hex())
    panel = loaded_window._poll_windows[(convo_id, survey_id)]
    await loaded_window.tally_vote(panel, {"s0": "yes"})
    await loaded_window.tally_close(panel)
    assert tally_presenter.survey_doc(convo_id, survey_id) is not None
    assert panel.current_survey() == (convo_id, survey_id)
    panel.close()


@pytest.mark.asyncio
async def test_a_vote_without_a_survey_does_nothing(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    votes: list[bytes] = []
    closes: list[bytes] = []

    async def record_vote(
        conversation_id: int,
        survey_id: bytes,
        choice: dict[str, str],
    ) -> bool:
        votes.append(survey_id)
        return True

    async def record_close(conversation_id: int, survey_id: bytes) -> bool:
        closes.append(survey_id)
        return True

    async def joined(conversation_id: int) -> bool:
        return True

    monkeypatch.setattr(katzen, "conversation_is_joined", joined)
    monkeypatch.setattr(katzen, "_io_tally_vote", record_vote)
    monkeypatch.setattr(katzen, "_io_tally_close", record_close)
    panel = katzen.TallyPanel(loaded_window)
    assert panel.current_survey() is None
    await loaded_window.tally_vote(panel, {})
    await loaded_window.tally_close(panel)
    assert votes == []
    assert closes == []
    panel.deleteLater()


@pytest.mark.asyncio
async def test_a_vote_before_joining_is_refused(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_joined(conversation_id: int) -> bool:
        return False

    monkeypatch.setattr(katzen, "conversation_is_joined", not_joined)
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Beer?",
        tally_schema.Mode.APPROVAL,
        ["yes"],
    )
    assert survey_id is not None
    loaded_window.openPoll(survey_id.hex())
    panel = loaded_window._poll_windows[(convo_id, survey_id)]
    await loaded_window.tally_vote(panel, {"s0": "yes"})
    await loaded_window.tally_close(panel)
    assert len(boxes.seen) == 2
    panel.close()


@pytest.mark.asyncio
async def test_a_failing_vote_and_close_are_reported(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def joined(conversation_id: int) -> bool:
        return True

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("stream is gone")

    monkeypatch.setattr(katzen, "conversation_is_joined", joined)
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Cake?",
        tally_schema.Mode.APPROVAL,
        ["yes"],
    )
    assert survey_id is not None
    loaded_window.openPoll(survey_id.hex())
    panel = loaded_window._poll_windows[(convo_id, survey_id)]
    monkeypatch.setattr(katzen, "_io_tally_vote", boom)
    monkeypatch.setattr(katzen, "_io_tally_close", boom)
    await loaded_window.tally_vote(panel, {"s0": "yes"})
    await loaded_window.tally_close(panel)
    assert [box.text for box in boxes.seen] == [
        "Could not send your vote:\nstream is gone",
        "Could not end the poll:\nstream is gone",
    ]
    panel.close()


@pytest.mark.asyncio
async def test_staging_a_tally_appends_an_optimistic_row(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Film?",
        tally_schema.Mode.APPROVAL,
        ["yes"],
    )
    assert survey_id is not None
    with persistent.Session(persistent._engine_sync) as sess:
        rows = sess.exec(
            persistent.select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id,
            )
        ).all()
    pending = [r for r in rows if r.network_status == 1]
    assert len(pending) == 1
    assert pending[0].payload[:1] == b"F"
