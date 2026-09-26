from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from collections.abc import Callable, Coroutine

import cbor2
import pytest
from sqlmodel import select

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, QPoint  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMenu,
    QMessageBox,
)

from katzenqt import katzen, network, persistent  # noqa: E402
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
from tests.stubs import ignore, returning

Pick = Callable[[QMenu], QAction | None]


@pytest.fixture()
def chosen(monkeypatch: pytest.MonkeyPatch) -> Callable[[Pick], None]:
    def install(picker: Pick) -> None:
        async def fake(menu: QMenu, pos: QPoint) -> QAction | None:
            return picker(menu)

        monkeypatch.setattr(katzen, "_menu_chosen", fake)

    return install


def by_text(label: str) -> Pick:
    def pick(menu: QMenu) -> QAction | None:
        for action in menu.actions():
            if action.text() == label:
                return action
        return None

    return pick


def nothing(menu: QMenu) -> QAction | None:
    return None


def peer_position(
    win: katzen.MainWindow,
    item: katzen.QStandardItem,
) -> QPoint:
    tree = win.ui.contacts_treeWidget
    source = win.all_contacts.indexFromItem(item)
    proxy = tree.model().mapFromSource(source)
    parent = proxy.parent()
    if parent.isValid():
        tree.expand(parent)
    point: QPoint = tree.visualRect(proxy).center()
    return point


def peer_named(win: katzen.MainWindow, name: str) -> katzen.QStandardItem:
    state = win.convo_state()
    item = state.contacts_standard_item
    for row in range(item.rowCount()):
        child = item.child(row)
        if child.text() == name:
            return child
    raise AssertionError(f"no peer row named {name}")


@pytest.fixture()
def pauses(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[uuid.UUID]]:
    calls: dict[str, list[uuid.UUID]] = {
        "pause_read": [],
        "resume_read": [],
        "pause_upload": [],
        "resume_upload": [],
        "cancel_upload": [],
        "dismiss": [],
    }

    def recorder(key: str, kwarg: str) -> Callable[..., object]:
        async def record(**kwargs: uuid.UUID) -> None:
            calls[key].append(kwargs[kwarg])

        return record

    monkeypatch.setattr(
        network,
        "pause_peer_reads",
        recorder("pause_read", "bacap_stream"),
    )
    monkeypatch.setattr(
        network,
        "resume_peer_reads",
        recorder("resume_read", "bacap_stream"),
    )
    monkeypatch.setattr(
        network,
        "pause_upload",
        recorder("pause_upload", "rcw_id"),
    )
    monkeypatch.setattr(
        network,
        "resume_upload",
        recorder("resume_upload", "rcw_id"),
    )
    monkeypatch.setattr(
        network,
        "cancel_upload",
        recorder("cancel_upload", "rcw_id"),
    )
    monkeypatch.setattr(
        network,
        "dismiss_failed_transfer",
        recorder("dismiss", "bacap_stream"),
    )
    return calls


@pytest.mark.asyncio
async def test_a_click_off_any_row_offers_no_peer_menu(
    loaded_window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    chosen(nothing)
    await loaded_window.peer_context_menu(QPoint(-5, -5))
    assert pauses["pause_read"] == []


@pytest.mark.asyncio
async def test_a_click_on_a_conversation_row_offers_no_peer_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    state = window.convo_state()
    pos = peer_position(window, state.contacts_standard_item)
    chosen(nothing)
    await window.peer_context_menu(pos)
    assert pauses["pause_read"] == []


@pytest.mark.asyncio
async def test_our_own_peer_row_offers_no_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    pos = peer_position(window, peer_named(window, "me"))
    await window.peer_context_menu(pos)
    assert pauses["pause_read"] == []


@pytest.mark.asyncio
async def test_pausing_a_peer_stops_reading_its_stream(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    bob = peer_named(window, "bob")
    chosen(by_text("Do not read from bob any more"))
    await window.peer_context_menu(peer_position(window, bob))
    assert pauses["pause_read"] == [bob.peer_read_cap_id]
    assert pauses["resume_read"] == []


@pytest.mark.asyncio
async def test_resuming_a_paused_peer_restarts_its_stream(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    bob = peer_named(window, "bob")
    with persistent.Session(persistent._engine_sync) as sess:
        rcw = sess.get(persistent.ReadCapWAL, bob.peer_read_cap_id)
        assert rcw is not None
        rcw.paused = True
        sess.add(rcw)
        sess.commit()
    chosen(by_text("Resume reading from bob"))
    await window.peer_context_menu(peer_position(window, bob))
    assert pauses["resume_read"] == [bob.peer_read_cap_id]
    assert pauses["pause_read"] == []


@pytest.mark.asyncio
async def test_dismissing_the_peer_menu_changes_nothing(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    window.show()
    window.resize(900, 600)
    chosen(nothing)
    await window.peer_context_menu(
        peer_position(window, peer_named(window, "bob")),
    )
    assert pauses["pause_read"] == []
    assert pauses["resume_read"] == []


def transfer_position(win: katzen.MainWindow, row: int = 0) -> QPoint:
    view = win.transfers_view
    point: QPoint = view.visualRect(view.model().index(row, 0)).center()
    return point


def add_download(win: katzen.MainWindow, stream: uuid.UUID) -> None:
    win.transfers_model.start_transfer(stream, 1, "bob", 4)
    win.transfers_view.resize(600, 200)


@pytest.mark.asyncio
async def test_a_click_off_any_transfer_row_offers_no_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    chosen(nothing)
    await window.transfers_context_menu(QPoint(-5, -5))
    assert pauses["pause_read"] == []


@pytest.mark.asyncio
async def test_pausing_a_download_from_the_transfers_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    stream = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ReadCapWAL(id=stream, read_cap=b"r" * 136))
        sess.add(
            persistent.ConversationPeer(
                name=":substream:1:aa",
                read_cap_id=stream,
                active=True,
            )
        )
        sess.commit()
    window.show()
    add_download(window, stream)
    chosen(by_text("Pause download"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["pause_read"] == [stream]


@pytest.mark.asyncio
async def test_resuming_a_paused_download_from_the_transfers_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    stream = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.ReadCapWAL(
                id=stream,
                read_cap=b"r" * 136,
                paused=True,
            )
        )
        sess.add(
            persistent.ConversationPeer(
                name=":substream:1:aa",
                read_cap_id=stream,
                active=True,
            )
        )
        sess.commit()
    window.show()
    add_download(window, stream)
    chosen(by_text("Resume download"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["resume_read"] == [stream]


@pytest.mark.asyncio
async def test_removing_a_failed_transfer_drops_its_row(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    stream = uuid.uuid4()
    window.show()
    add_download(window, stream)
    window.transfers_model.fail_transfer(stream, "missing box")
    chosen(by_text("Remove"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["dismiss"] == [stream]
    assert window.transfers_model.rowCount() == 0


@pytest.mark.asyncio
async def test_dismissing_the_failed_transfer_menu_keeps_the_row(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    stream = uuid.uuid4()
    window.show()
    add_download(window, stream)
    window.transfers_model.fail_transfer(stream, "missing box")
    chosen(nothing)
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["dismiss"] == []
    assert window.transfers_model.rowCount() == 1


@pytest.mark.asyncio
async def test_pausing_and_resuming_an_upload(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
) -> None:
    stream = uuid.uuid4()
    window.show()
    window.transfers_model.start_transfer(
        stream,
        1,
        "pic.png (in bob)",
        4,
        direction="upload",
        raw_bytes=99,
    )
    window.transfers_view.resize(600, 200)
    chosen(by_text("Pause upload"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["pause_upload"] == [stream]

    window.transfers_model.set_paused(stream, paused=True)
    chosen(by_text("Resume upload"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["resume_upload"] == [stream]


@pytest.mark.asyncio
async def test_cancelling_an_upload_needs_confirmation(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def refuse(box: FakeMessageBox) -> QMessageBox.StandardButton:
        prompts.append(box.text)
        return FakeMessageBox.StandardButton.No

    monkeypatch.setattr(katzen, "_dialog_finished", refuse)
    stream = uuid.uuid4()
    window.show()
    window.transfers_model.start_transfer(
        stream,
        1,
        "pic.png",
        4,
        direction="upload",
    )
    window.transfers_view.resize(600, 200)
    chosen(by_text("Cancel upload"))
    await window.transfers_context_menu(transfer_position(window))
    assert prompts == [
        "Cancel this upload? Its message is removed from the conversation.",
    ]
    assert pauses["cancel_upload"] == []


@pytest.mark.asyncio
async def test_a_confirmed_upload_cancel_is_dispatched(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    pauses: dict[str, list[uuid.UUID]],
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def accept(box: FakeMessageBox) -> QMessageBox.StandardButton:
        return FakeMessageBox.StandardButton.Yes

    monkeypatch.setattr(katzen, "_dialog_finished", accept)
    stream = uuid.uuid4()
    window.show()
    window.transfers_model.start_transfer(
        stream,
        1,
        "pic.png",
        4,
        direction="upload",
    )
    window.transfers_view.resize(600, 200)
    chosen(by_text("Cancel upload"))
    await window.transfers_context_menu(transfer_position(window))
    assert pauses["cancel_upload"] == [stream]


@pytest.mark.asyncio
async def test_the_test_shortcut_round_trips_a_keypair(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tested: list[tuple[bytes, bytes]] = []

    def create(seed: bytes) -> tuple[bytes, bytes]:
        return b"write", b"read"

    async def test_keypair(
        client: object,
        write_cap: bytes,
        read_cap: bytes,
    ) -> None:
        tested.append((write_cap, read_cap))

    monkeypatch.setattr(network, "create_new_keypair", create)
    monkeypatch.setattr(network, "test_keypair", test_keypair)
    await window.testme()
    assert tested == [(b"write", b"read")]


def test_the_nanosecond_clock_falls_back_to_monotonic_ns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoRawClock:
        @staticmethod
        def monotonic_ns() -> int:
            return 1234

    with monkeypatch.context() as scoped:
        scoped.setattr(katzen, "time", NoRawClock)
        assert katzen.duration_time_ns() == 1234


@pytest.mark.asyncio
async def test_a_substream_peer_never_reaches_the_contacts_tree(
    window: katzen.MainWindow,
) -> None:
    hidden = f"{network._SUBSTREAM_NAME_PREFIX}1:aa"
    seeded = await seed_conversation(peers=("bob", hidden))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    item = window.conversation_state_by_id[
        seeded.conversation_id
    ].contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert sorted(names) == ["bob", "me"]


@pytest.mark.asyncio
async def test_a_cancelled_display_name_prompt_creates_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [1, 0]

    async def answer(dialog: katzen.QInputDialog) -> int:
        result = answers.pop(0)
        if result:
            dialog.setTextValue("Reading room")
        return result

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await window.new_conversation()
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_new_poll_without_a_conversation_does_nothing(
    window: katzen.MainWindow,
) -> None:
    await window.new_poll()
    assert window._poll_windows == {}


@pytest.mark.asyncio
async def test_the_join_loop_waits_for_the_daemon_connection(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window.iothread.kp_client = None

    async def open_it(client: object, conversation_id: int) -> list[str]:
        return ["zoe"]

    def connect_later() -> None:
        window.iothread.kp_client = object()

    monkeypatch.setattr(katzen, "await_and_open", open_it)
    asyncio.get_running_loop().call_soon(connect_later)
    assert await window._wait_and_open_with_retries(1) == ["zoe"]


@pytest.mark.asyncio
async def test_a_body_that_is_not_cbor_at_all_is_not_an_attachment(
    window: katzen.MainWindow,
) -> None:
    message_uuid = uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.ConversationLog(
                id=message_uuid,
                conversation_id=1,
                conversation_peer_id=1,
                conversation_order=0,
                payload=b"F\x9f",
            )
        )
        sess.commit()
    assert window._resolve_attachment(str(message_uuid)) is None


@pytest.mark.asyncio
async def test_a_sent_attachment_whose_source_moved_is_refused(
    window: katzen.MainWindow,
    tmp_path: Path,
) -> None:
    message_uuid = uuid.uuid4()
    gone = tmp_path / "moved-away.txt"
    payload = b"F" + cbor2.dumps(
        {
            "kind": "file_outgoing",
            "basename": "moved-away.txt",
            "filetype": "text/plain",
            "src_path": str(gone),
        }
    )
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.ConversationLog(
                id=message_uuid,
                conversation_id=1,
                conversation_peer_id=1,
                conversation_order=0,
                payload=payload,
                network_status=2,
            )
        )
        sess.commit()
    with pytest.raises(katzen._AttachmentError) as caught:
        window._resolve_attachment(str(message_uuid))
    assert str(caught.value) == (
        f"The original file for moved-away.txt is no longer available at "
        f"{gone}."
    )


@pytest.mark.asyncio
async def test_push_to_talk_without_an_engine_records_nothing(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loaded_window, "_push_to_talk_audio", ignore)
    loaded_window.push_to_talk_start()
    assert loaded_window.push_to_talk_started is False


@pytest.mark.asyncio
async def test_an_update_for_a_conversation_that_never_appears_is_dropped(
    window: katzen.MainWindow,
) -> None:
    await window._process_conversation_update(4242, False)
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_peer_for_a_conversation_that_never_appears_is_dropped(
    window: katzen.MainWindow,
) -> None:
    await window._process_peer_added(4242, "ghost")
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_poll_window_whose_survey_vanished_is_left_alone(
    loaded_window: katzen.MainWindow,
) -> None:
    convo_id = loaded_window.convo_state().conversation_id
    survey_id = await katzen._io_tally_create(
        convo_id,
        "Gone?",
        tally_schema.Mode.APPROVAL,
        ["yes"],
    )
    assert survey_id is not None
    loaded_window.openPoll(survey_id.hex())
    panel = loaded_window._poll_windows[(convo_id, survey_id)]
    panel.hide()
    with persistent.Session(persistent._engine_sync) as sess:
        for row in sess.exec(persistent.select(persistent.TallyState)).all():
            sess.delete(row)
        sess.commit()
    loaded_window._open_poll_window(convo_id, survey_id)
    assert panel.isVisible() is False
    panel.close()


@pytest.mark.asyncio
async def test_a_joined_member_already_in_the_database_is_tagged(
    window: katzen.MainWindow,
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await seed_conversation(peers=("carol",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)

    async def opened(conversation_id: int, delay: float = 2.0) -> list[str]:
        return ["carol"]

    async def signal() -> None:
        return None

    monkeypatch.setattr(window, "_wait_and_open_with_retries", opened)
    monkeypatch.setattr(network, "signal_readables_to_mixwal", signal)
    convo = window.convo_state()
    await window._await_voucher_join(convo)
    item = convo.contacts_standard_item
    tagged = item.child(item.rowCount() - 1)
    assert tagged.text() == "carol"
    assert tagged.peer_is_own is False
    assert isinstance(tagged.peer_read_cap_id, uuid.UUID)


@pytest.mark.asyncio
async def test_an_inducted_member_already_in_the_database_is_tagged(
    window: katzen.MainWindow,
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await seed_conversation(peers=("carol",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)

    async def answer(dialog: katzen.QInputDialog) -> int:
        dialog.setTextValue("dm91Y2hlcg==")
        return 1

    async def induct(
        client: object,
        conversation_id: int,
        name: str,
        voucher: bytes,
    ) -> str:
        return "carol"

    async def signal() -> None:
        return None

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    monkeypatch.setattr(katzen, "derive_read_and_induct", induct)
    monkeypatch.setattr(network, "signal_readables_to_mixwal", signal)
    await window.induct_via_voucher()
    item = window.convo_state().contacts_standard_item
    tagged = item.child(item.rowCount() - 1)
    assert tagged.text() == "carol"
    assert tagged.peer_is_own is False
    assert isinstance(tagged.peer_read_cap_id, uuid.UUID)


@pytest.mark.asyncio
async def test_selecting_nothing_leaves_the_chat_view_alone(
    loaded_window: katzen.MainWindow,
) -> None:
    before = loaded_window.ui.ContactName.text()
    await loaded_window.conversation_selected(None, None)
    assert loaded_window.ui.ContactName.text() == before


def answering(
    verdict: bool,
) -> Callable[[QDialog], "Coroutine[object, object, int]"]:
    async def answer(dialog: QDialog) -> int:
        if verdict:
            return int(QMessageBox.StandardButton.Yes)
        return int(QMessageBox.StandardButton.No)

    return answer


@pytest.fixture()
def confirms(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def install(verdict: bool) -> None:
        monkeypatch.setattr(katzen, "_dialog_finished", answering(verdict))

    return install


async def _shown(win: katzen.MainWindow, peers: tuple[str, ...] = ()) -> int:
    seeded = await seed_conversation(peers=peers)
    await add_seeded_conversation(win, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    win.show()
    win.resize(900, 600)
    return seeded.conversation_id


def conversation_row(win: katzen.MainWindow) -> katzen.QStandardItem:
    return win.convo_state().contacts_standard_item


@pytest.mark.asyncio
async def test_removing_a_group_chat_from_its_row_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
) -> None:
    conv_id = await _shown(window)
    confirms(True)
    chosen(by_text("Remove group chat..."))
    await window.peer_context_menu(
        peer_position(window, conversation_row(window))
    )

    assert conv_id not in window.conversation_state_by_id
    async with persistent.asession() as sess:
        assert await sess.get(persistent.Conversation, conv_id) is None


@pytest.mark.asyncio
async def test_declining_the_group_chat_removal_keeps_it(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
) -> None:
    conv_id = await _shown(window)
    confirms(False)
    chosen(by_text("Remove group chat..."))
    await window.peer_context_menu(
        peer_position(window, conversation_row(window))
    )

    assert conv_id in window.conversation_state_by_id
    async with persistent.asession() as sess:
        assert await sess.get(persistent.Conversation, conv_id) is not None


@pytest.mark.asyncio
async def test_a_failed_group_chat_removal_is_reported(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conv_id = await _shown(window)
    reported: list[BaseException] = []
    monkeypatch.setattr(window, "_report_removal_failure", reported.append)

    async def explode(*, conversation_id: int) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(katzen.removal, "remove_conversation", explode)
    confirms(True)
    chosen(by_text("Remove group chat..."))
    await window.peer_context_menu(
        peer_position(window, conversation_row(window))
    )

    assert [type(e) for e in reported] == [RuntimeError]
    assert conv_id in window.conversation_state_by_id


@pytest.mark.asyncio
async def test_removing_a_peer_from_its_row_menu(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
) -> None:
    await _shown(window, peers=("bob",))
    bob = peer_named(window, "bob")
    confirms(True)
    chosen(by_text("Remove bob from this group chat..."))
    await window.peer_context_menu(peer_position(window, bob))

    async with persistent.asession() as sess:
        names = [
            p.name
            for p in (
                await sess.exec(select(persistent.ConversationPeer))
            ).all()
        ]
    assert "bob" not in names


@pytest.mark.asyncio
async def test_declining_the_peer_removal_keeps_them(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
) -> None:
    await _shown(window, peers=("bob",))
    bob = peer_named(window, "bob")
    confirms(False)
    chosen(by_text("Remove bob from this group chat..."))
    await window.peer_context_menu(peer_position(window, bob))

    async with persistent.asession() as sess:
        names = [
            p.name
            for p in (
                await sess.exec(select(persistent.ConversationPeer))
            ).all()
        ]
    assert "bob" in names


@pytest.mark.asyncio
async def test_a_peer_row_naming_no_database_row_removes_nothing(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _shown(window, peers=("bob",))
    bob = peer_named(window, "bob")
    asked: list[str] = []
    monkeypatch.setattr(window, "_peer_id_of", returning(None))

    async def record(text: str) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr(window, "_confirm", record)
    chosen(by_text("Remove bob from this group chat..."))
    await window.peer_context_menu(peer_position(window, bob))

    assert asked == []


@pytest.mark.asyncio
async def test_a_failed_peer_removal_is_reported(
    window: katzen.MainWindow,
    chosen: Callable[[Pick], None],
    confirms: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _shown(window, peers=("bob",))
    bob = peer_named(window, "bob")
    reported: list[BaseException] = []
    monkeypatch.setattr(window, "_report_removal_failure", reported.append)

    async def explode(*, conversation_id: int, peer_id: int) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(katzen.removal, "remove_peer", explode)
    confirms(True)
    chosen(by_text("Remove bob from this group chat..."))
    await window.peer_context_menu(peer_position(window, bob))

    assert [type(e) for e in reported] == [RuntimeError]
    assert peer_named(window, "bob") is not None


@pytest.mark.asyncio
async def test_a_removal_failure_reaches_a_message_box(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    window._report_removal_failure(RuntimeError("the database said no"))

    assert [kind for kind, _title, _text in boxes.seen] == ["critical"]
    assert "the database said no" in boxes.seen[0][2]


@pytest.mark.asyncio
async def test_dropping_a_peer_row_of_an_unknown_chat_touches_no_model(
    window: katzen.MainWindow,
) -> None:
    await _shown(window, peers=("bob",))
    bob = peer_named(window, "bob")
    conversation = conversation_row(window)
    window.conversation_state_by_id.clear()

    window._drop_peer_ui(conversation, bob)

    remaining = [
        conversation.child(row).text()
        for row in range(conversation.rowCount())
    ]
    assert "bob" not in remaining


@pytest.mark.asyncio
async def test_selecting_a_row_whose_chat_is_gone_is_ignored(
    window: katzen.MainWindow,
) -> None:
    await _shown(window)
    tree = window.ui.contacts_treeWidget
    source = window.all_contacts.indexFromItem(conversation_row(window))
    proxy = tree.model().mapFromSource(source)
    window.conversation_state_by_id.clear()

    await window.conversation_selected(proxy, QModelIndex())

    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_row_the_proxy_cannot_map_back_names_no_contact(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _shown(window)
    tree = window.ui.contacts_treeWidget
    pos = peer_position(window, conversation_row(window))
    monkeypatch.setattr(
        tree.model(),
        "mapToSource",
        returning(QModelIndex()),
    )

    assert window._contact_item_at(pos) is None
