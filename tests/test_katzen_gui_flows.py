from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

from katzenqt import katzen, network, persistent  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    PNG_BYTES,
    FakeAudio,
    FakeFileDialog,
    FakeMessageBox,
    add_seeded_conversation,
    audio,
    boxes,
    instant_timer,
    is_own,
    loaded_window,
    proxy_of,
    qt_app,
    seed_conversation,
    systray_of,
    window,
)
from tests.stubs import appending_from, first_argument, ignore, returning

Sent = list[dict[str, object]]


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    recorded: Sent = []

    async def record(**kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(network, "notify_outbound_chat_sent", record)
    monkeypatch.setattr(network, "notify_outbound_text_sent", record)
    return recorded


@pytest.fixture()
def joined(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yes(conversation_id: int) -> bool:
        return True

    monkeypatch.setattr(katzen, "conversation_is_joined", yes)


@pytest.fixture()
def not_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no(conversation_id: int) -> bool:
        return False

    monkeypatch.setattr(katzen, "conversation_is_joined", no)


def queue(win: katzen.MainWindow, *paths: Path) -> None:
    convo = win.convo_state()
    convo.attached_files |= {str(p) for p in paths}
    win.refresh_attached_files_for_conversation(convo)


def marker_of(entry: dict[str, object]) -> dict[str, object]:
    payload = entry["payload"]
    assert isinstance(payload, bytes)
    decoded = cbor2.loads(payload[1:])
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.asyncio
async def test_a_send_before_joining_is_refused_and_keeps_the_text(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    loaded_window.ui.chat_lineEdit.setText("hello there")
    await loaded_window.chat_msg_single_line()
    assert boxes.seen[0].text.startswith(
        "You have not joined this conversation yet.",
    )
    assert loaded_window.ui.chat_lineEdit.text() == "hello there"


@pytest.mark.asyncio
async def test_a_refused_send_buffers_the_text_for_a_background_convo(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    instant_timer: None,
) -> None:
    convo = loaded_window.convo_state()
    loaded_window.ui.chat_lineEdit.setText("draft text")
    loaded_window._restore_unsent_text(convo, "draft text")
    assert loaded_window.ui.chat_lineEdit.text() == "draft text"
    other = katzen.ConversationUIState(
        conversation_id=999,
        own_peer_id=1,
        own_peer_name="x",
        own_peer_bacap_uuid=uuid.uuid4(),
        chat_lineEdit_buffer="",
        conversation_log_model=convo.conversation_log_model,
        contacts_standard_item=convo.contacts_standard_item,
    )
    loaded_window._restore_unsent_text(other, "other text")
    assert other.chat_lineEdit_buffer == "other text"


@pytest.mark.asyncio
async def test_an_empty_line_sends_nothing(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
) -> None:
    loaded_window.ui.chat_lineEdit.setText("   ")
    await loaded_window.chat_msg_single_line()
    assert sent == []
    assert loaded_window.convo_state().chat_lineEdit_buffer == ""


@pytest.mark.asyncio
async def test_a_single_line_message_reaches_the_send_path(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
) -> None:
    loaded_window.ui.chat_lineEdit.setText("hello mixnet")
    await loaded_window.chat_msg_single_line()
    assert len(sent) == 1
    gcm = sent[0]["gcm"]
    assert isinstance(gcm, katzen.GroupChatMessage)
    assert gcm.text == "hello mixnet"
    assert sent[0]["conversation_id"] == (
        loaded_window.convo_state().conversation_id
    )
    assert loaded_window.ui.chat_lineEdit.text() == ""


@pytest.mark.asyncio
async def test_sending_files_before_joining_keeps_them_queued(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    sent: Sent,
    tmp_path: Path,
    instant_timer: None,
) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"body")
    queue(loaded_window, doc)
    await loaded_window.send_file()
    assert sent == []
    assert loaded_window.convo_state().attached_files == {str(doc)}


@pytest.mark.asyncio
async def test_sending_a_plain_file_stores_a_lightweight_marker(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
    tmp_path: Path,
) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_bytes(b"body bytes")
    queue(loaded_window, doc)
    await loaded_window.send_file()
    assert len(sent) == 1
    marker = marker_of(sent[0])
    assert marker["kind"] == "file_outgoing"
    assert marker["basename"] == "doc.txt"
    assert marker["size"] == 10
    assert marker["src_path"] == str(doc)
    assert "thumb_rel_path" not in marker
    assert loaded_window.convo_state().attached_files == set()
    assert loaded_window.ui.attached_files_QListWidget.count() == 0


@pytest.mark.asyncio
async def test_sending_an_image_also_spills_a_thumbnail(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
    tmp_path: Path,
) -> None:
    picture = tmp_path / "pic.png"
    picture.write_bytes(PNG_BYTES)
    queue(loaded_window, picture)
    await loaded_window.send_file()
    marker = marker_of(sent[0])
    assert marker["filetype"] == "image/png"
    thumb = marker["thumb_rel_path"]
    assert isinstance(thumb, str)
    assert (persistent.state_file.parent / thumb).is_file() is True


@pytest.mark.asyncio
async def test_a_vanished_attachment_is_reported_and_skipped(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "gone.txt"
    loaded_window.convo_state().attached_files.add(str(missing))
    await loaded_window.send_file()
    assert sent == []
    assert boxes.seen[0].text == (
        f"Attachment no longer exists and was skipped:\n{missing}"
    )


@pytest.mark.asyncio
async def test_an_oversized_attachment_is_refused(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(network, "_ATTACHMENT_HARD_CAP", 4)
    fat = tmp_path / "fat.bin"
    fat.write_bytes(b"0123456789")
    queue(loaded_window, fat)
    await loaded_window.send_file()
    assert sent == []
    assert "exceeds the 0 MiB attachment limit" in boxes.seen[0].text
    assert boxes.seen[0].text.startswith("fat.bin is 0.0 MiB, which ")


@pytest.mark.asyncio
async def test_sending_a_voice_note_caches_it_and_drops_the_draft(
    loaded_window: katzen.MainWindow,
    joined: None,
    sent: Sent,
    audio: FakeAudio,
) -> None:
    draft = audio.start_capture(5)
    queue(loaded_window, draft)
    await loaded_window.send_file()
    marker = marker_of(sent[0])
    assert marker["filetype"] == "audio/opus"
    assert marker["src_path"] != str(draft)
    assert Path(str(marker["src_path"])).parent == audio.received_dir
    assert audio.discarded == [draft]


@pytest.mark.asyncio
async def test_attaching_files_adds_them_to_the_conversation(
    loaded_window: katzen.MainWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_bytes(b"1")
    second.write_bytes(b"22")
    FakeFileDialog.files = [str(first), str(second)]
    monkeypatch.setattr(katzen, "QFileDialog", FakeFileDialog)
    loaded_window.attach_file()
    assert loaded_window.convo_state().attached_files == {
        str(first),
        str(second),
    }
    assert loaded_window.saved_file_dialog == b"dialog-state"
    assert loaded_window.ui.attached_files_QListWidget.count() == 2

    loaded_window.attach_file()
    assert FakeFileDialog.restored == [b"dialog-state"]


@pytest.mark.asyncio
async def test_cancelling_the_file_picker_attaches_nothing(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFileDialog.accepted = False
    monkeypatch.setattr(katzen, "QFileDialog", FakeFileDialog)
    loaded_window.attach_file()
    assert loaded_window.convo_state().attached_files == set()
    assert loaded_window.saved_file_dialog == b"dialog-state"


@pytest.mark.asyncio
async def test_selecting_a_conversation_fills_the_chat_view(
    loaded_window: katzen.MainWindow,
) -> None:
    convo = loaded_window.convo_state()
    assert loaded_window.ui.ContactName.text() == (
        f"testroom (your name: {convo.own_peer_name})"
    )
    assert loaded_window.ui.attach_file_tab.isEnabled() is True
    assert loaded_window.ui.attach_file_button.isEnabled() is True
    assert loaded_window.ui.poll_tab.isEnabled() is True
    assert loaded_window.ui.new_poll_button.isEnabled() is True
    assert systray_of(loaded_window).read_messages >= 1
    assert loaded_window.ui.qml_ChatLines.rootObject() is not None


@pytest.mark.asyncio
async def test_switching_conversations_stores_the_previous_draft(
    loaded_window: katzen.MainWindow,
) -> None:
    first = loaded_window.convo_state()
    loaded_window.ui.chat_lineEdit.setText("half-written")
    second = await seed_conversation(name="second room", own_name="me2")
    await add_seeded_conversation(loaded_window, second.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    assert loaded_window.convo_state().conversation_id == (
        second.conversation_id
    )
    assert first.chat_lineEdit_buffer == "half-written"
    assert loaded_window.ui.chat_lineEdit.text() == ""
    assert loaded_window.ui.ContactName.text().startswith("second room")


@pytest.mark.asyncio
async def test_selecting_a_peer_row_selects_its_conversation(
    window: katzen.MainWindow,
) -> None:
    seeded = await seed_conversation(peers=("bob",))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    convo_item = window.conversation_state_by_id[
        seeded.conversation_id
    ].contacts_standard_item
    names = [convo_item.child(r).text() for r in range(convo_item.rowCount())]
    assert [n for n in sorted(names) if n != "me"] == ["bob"]
    bob = next(
        convo_item.child(r)
        for r in range(convo_item.rowCount())
        if convo_item.child(r).text() == "bob"
    )
    assert is_own(bob) is False
    source = window.all_contacts.indexFromItem(bob)
    proxy = proxy_of(window).mapFromSource(source)
    window.ui.contacts_treeWidget.setCurrentIndex(proxy)
    for _ in range(80):
        await asyncio.sleep(0)
    assert window.convo_state().conversation_id == seeded.conversation_id


@pytest.mark.asyncio
async def test_two_peers_sharing_a_name_each_get_their_own_row(
    window: katzen.MainWindow,
) -> None:
    seeded = await seed_conversation(peers=("bob", "bob"))
    await add_seeded_conversation(window, seeded.conversation_id)
    for _ in range(80):
        await asyncio.sleep(0)
    item = window.conversation_state_by_id[
        seeded.conversation_id
    ].contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert len([n for n in names if n.startswith("bob")]) == 2


@pytest.mark.asyncio
async def test_creating_a_conversation_walks_both_prompts(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = ["Book club", "reader"]

    async def answer(dialog: object) -> int:
        if isinstance(dialog, QInputDialog):
            dialog.setTextValue(answers.pop(0))
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await window.new_conversation()
    for _ in range(80):
        await asyncio.sleep(0)
    assert len(window.conversation_state_by_id) == 1
    state = next(iter(window.conversation_state_by_id.values()))
    assert state.own_peer_name == "reader"
    assert window.ui.ContactName.text() == "Book club (your name: reader)"
    with persistent.Session(persistent._engine_sync) as sess:
        convo = sess.get(persistent.Conversation, state.conversation_id)
        assert convo is not None
        assert convo.name == "Book club"
        assert convo.first_unread == 0


@pytest.mark.asyncio
async def test_a_cancelled_title_prompt_creates_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(dialog: object) -> int:
        return 0

    monkeypatch.setattr(katzen, "_dialog_finished", refuse)
    await window.new_conversation()
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_blank_title_creates_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blank(dialog: object) -> int:
        if isinstance(dialog, QInputDialog):
            dialog.setTextValue("   ")
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", blank)
    await window.new_conversation()
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_a_blank_display_name_creates_nothing(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = ["Reading room", "  "]

    async def answer(dialog: object) -> int:
        if isinstance(dialog, QInputDialog):
            dialog.setTextValue(answers.pop(0))
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await window.new_conversation()
    assert window.conversation_state_by_id == {}


@pytest.mark.asyncio
async def test_generating_a_voucher_without_a_conversation_complains(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    await window.generate_voucher()
    assert boxes.seen[0].text == (
        "Select a conversation first (or create one) before generating a "
        "voucher."
    )


@pytest.mark.asyncio
async def test_a_joined_conversation_needs_no_voucher(
    loaded_window: katzen.MainWindow,
    joined: None,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    await loaded_window.generate_voucher()
    assert boxes.seen[0].text.startswith(
        "You are already a member of this conversation",
    )


@pytest.fixture()
def voucher_flow(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []

    async def no_pending(conversation_id: int) -> None:
        log.append("pending_voucher_for")
        return None

    async def mint(
        client: object,
        conversation_id: int,
        display_name: str,
    ) -> bytes:
        log.append(f"mint:{display_name}")
        return b"voucher-bytes"

    monkeypatch.setattr(katzen, "pending_voucher_for", no_pending)
    monkeypatch.setattr(katzen, "mint_and_publish", mint)
    return log


@pytest.mark.asyncio
async def test_generating_a_voucher_shows_the_code(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    voucher_flow: list[str],
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervised: list[str] = []
    monkeypatch.setattr(
        loaded_window,
        "_supervised_listener",
        appending_from(supervised, first_argument),
    )

    name = loaded_window.convo_state().own_peer_name
    shown: list[str] = []

    async def answer(dialog: object) -> int:
        if isinstance(dialog, QInputDialog):
            dialog.setTextValue(name)
        label = getattr(dialog, "label", None)
        if label is not None:
            shown.append(label.text())
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await loaded_window.generate_voucher()
    assert voucher_flow == ["pending_voucher_for", f"mint:{name}"]
    texts = [box.text for box in boxes.seen] + shown
    assert any("dm91Y2hlci1ieXRlcw==" in t for t in texts)
    assert any(t.startswith(f"Here is your voucher, {name}.") for t in texts)
    assert supervised == ["_await_voucher_join"]


@pytest.mark.asyncio
async def test_a_pending_voucher_is_replaced_only_on_confirmation(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_id = uuid.uuid4()
    cancelled: list[uuid.UUID] = []
    offered: list[str] = []

    async def pending(conversation_id: int) -> uuid.UUID:
        return pending_id

    async def cancel(pv_id: uuid.UUID) -> None:
        cancelled.append(pv_id)

    async def refuse(
        dialog: FakeMessageBox,
    ) -> katzen.QMessageBox.StandardButton:
        offered.append(dialog.text)
        return FakeMessageBox.StandardButton.No

    monkeypatch.setattr(katzen, "pending_voucher_for", pending)
    monkeypatch.setattr(katzen, "cancel_pending_voucher", cancel)
    monkeypatch.setattr(katzen, "_dialog_finished", refuse)
    await loaded_window.generate_voucher()
    assert cancelled == []
    assert offered == [
        "A voucher for this conversation is already pending. Cancel it and "
        "generate a new one?",
    ]


@pytest.mark.asyncio
async def test_a_failed_mint_is_reported(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_pending(conversation_id: int) -> None:
        return None

    async def mint(
        client: object,
        conversation_id: int,
        display_name: str,
    ) -> bytes:
        raise RuntimeError("courier refused")

    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("newcomer")
        return 1

    monkeypatch.setattr(katzen, "pending_voucher_for", no_pending)
    monkeypatch.setattr(katzen, "mint_and_publish", mint)
    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await loaded_window.generate_voucher()
    assert boxes.seen[0].text.startswith("Could not mint the voucher.")
    assert "courier refused" in boxes.seen[0].text


@pytest.mark.asyncio
async def test_a_blank_voucher_name_stops_the_flow(
    loaded_window: katzen.MainWindow,
    not_joined: None,
    voucher_flow: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_window.convo_state().own_peer_name = ""

    async def answer(dialog: object) -> int:
        if isinstance(dialog, QInputDialog):
            dialog.setTextValue("")
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await loaded_window.generate_voucher()
    assert voucher_flow == ["pending_voucher_for"]


@pytest.mark.asyncio
async def test_a_completed_join_lists_the_new_members(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = f"{network._SUBSTREAM_NAME_PREFIX}parent:00ff"
    signalled: list[int] = []

    async def opened(conversation_id: int, delay: float = 2.0) -> list[str]:
        return ["carol", hidden]

    async def signal() -> None:
        signalled.append(1)

    monkeypatch.setattr(
        loaded_window,
        "_wait_and_open_with_retries",
        opened,
    )
    monkeypatch.setattr(network, "signal_readables_to_mixwal", signal)
    convo = loaded_window.convo_state()
    await loaded_window._await_voucher_join(convo)
    item = convo.contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert [n for n in names if n != convo.own_peer_name] == ["carol"]
    assert signalled == [1]
    assert boxes.seen[-1].text == "You have joined. Members added: carol."


@pytest.mark.asyncio
async def test_a_failed_join_is_reported_with_a_bounded_detail(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fails(conversation_id: int, delay: float = 2.0) -> list[str]:
        raise ValueError("<b>hostile</b> reply")

    monkeypatch.setattr(loaded_window, "_wait_and_open_with_retries", fails)
    await loaded_window._await_voucher_join(loaded_window.convo_state())
    assert boxes.seen[0].text == (
        "The voucher join did not complete:\nValueError: bhostile/b reply"
    )


@pytest.mark.asyncio
async def test_the_join_retry_loop_returns_the_opened_members(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def open_it(client: object, conversation_id: int) -> list[str]:
        return ["dave"]

    monkeypatch.setattr(katzen, "await_and_open", open_it)
    added = await loaded_window._wait_and_open_with_retries(1)
    assert added == ["dave"]


@pytest.mark.asyncio
async def test_the_join_retry_loop_gives_up_after_five_attempts(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[int] = []

    async def offline(client: object, conversation_id: int) -> list[str]:
        attempts.append(1)
        raise BrokenPipeError("daemon went away")

    monkeypatch.setattr(katzen, "await_and_open", offline)
    caplog.set_level(logging.WARNING)
    with pytest.raises(ConnectionError) as caught:
        await loaded_window._wait_and_open_with_retries(1, delay=0.0)
    assert len(attempts) == 5
    assert "could not reach the daemon after retries" in str(caught.value)


@pytest.mark.asyncio
async def test_inducting_without_a_conversation_complains(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
) -> None:
    await window.induct_via_voucher()
    assert boxes.seen[0].text == (
        "Create or select a conversation before inducting a contact."
    )


@pytest.mark.asyncio
async def test_a_malformed_voucher_code_is_refused(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("not base64!!!")
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await loaded_window.induct_via_voucher()
    assert boxes.seen[0].text == "Invalid voucher code."


@pytest.mark.asyncio
async def test_an_empty_voucher_code_stops_the_flow(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("")
        return 1

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    await loaded_window.induct_via_voucher()
    assert boxes.seen == []


@pytest.mark.asyncio
async def test_a_cancelled_voucher_prompt_stops_the_flow(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(dialog: object) -> int:
        return 0

    monkeypatch.setattr(katzen, "_dialog_finished", refuse)
    await loaded_window.induct_via_voucher()
    assert boxes.seen == []


@pytest.mark.asyncio
async def test_an_induction_adds_the_joiner_to_the_tree(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled: list[int] = []

    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("dm91Y2hlcg==")
        return 1

    async def induct(
        client: object,
        conversation_id: int,
        name: str,
        voucher: bytes,
    ) -> str:
        assert voucher == b"voucher"
        return "erin"

    async def signal() -> None:
        signalled.append(1)

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    monkeypatch.setattr(katzen, "derive_read_and_induct", induct)
    monkeypatch.setattr(network, "signal_readables_to_mixwal", signal)
    convo = loaded_window.convo_state()
    await loaded_window.induct_via_voucher()
    item = convo.contacts_standard_item
    names = [item.child(r).text() for r in range(item.rowCount())]
    assert [n for n in names if n != convo.own_peer_name] == ["erin"]
    assert signalled == [1]
    assert boxes.seen[-1].text == "Inducted erin into this conversation."


@pytest.mark.asyncio
async def test_a_repeated_induction_says_so(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("dm91Y2hlcg==")
        return 1

    async def induct(
        client: object,
        conversation_id: int,
        name: str,
        voucher: bytes,
    ) -> None:
        return None

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    monkeypatch.setattr(katzen, "derive_read_and_induct", induct)
    await loaded_window.induct_via_voucher()
    assert boxes.seen[-1].text == "This contact was already inducted."


@pytest.mark.asyncio
async def test_a_failed_induction_is_reported(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("dm91Y2hlcg==")
        return 1

    async def induct(
        client: object,
        conversation_id: int,
        name: str,
        voucher: bytes,
    ) -> str:
        raise RuntimeError("no such voucher\x07")

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    monkeypatch.setattr(katzen, "derive_read_and_induct", induct)
    await loaded_window.induct_via_voucher()
    assert boxes.seen[-1].text == (
        "Induction failed:\nRuntimeError: no such voucher"
    )


@pytest.mark.asyncio
async def test_an_inducted_substream_peer_is_not_rendered(
    loaded_window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answer(dialog: QInputDialog) -> int:
        dialog.setTextValue("dm91Y2hlcg==")
        return 1

    async def induct(
        client: object,
        conversation_id: int,
        name: str,
        voucher: bytes,
    ) -> str:
        return f"{network._SUBSTREAM_NAME_PREFIX}parent:1234"

    async def signal() -> None:
        return None

    monkeypatch.setattr(katzen, "_dialog_finished", answer)
    monkeypatch.setattr(katzen, "derive_read_and_induct", induct)
    monkeypatch.setattr(network, "signal_readables_to_mixwal", signal)
    convo = loaded_window.convo_state()
    before = convo.contacts_standard_item.rowCount()
    await loaded_window.induct_via_voucher()
    assert convo.contacts_standard_item.rowCount() == before


@pytest.mark.asyncio
async def test_no_pending_vouchers_says_so(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def none() -> list[object]:
        return []

    monkeypatch.setattr(katzen, "list_pending_vouchers", none)
    await window.show_pending_vouchers()
    assert boxes.seen[0].text == "There are no pending vouchers."


@pytest.mark.asyncio
async def test_abandoning_a_pending_voucher_cancels_it(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pv_id = uuid.uuid4()
    cancelled: list[uuid.UUID] = []

    async def listing() -> list[tuple[uuid.UUID, str, str, str]]:
        return [(pv_id, "room", "joiner", "minted")]

    async def cancel(target: uuid.UUID) -> None:
        cancelled.append(target)

    async def close_it(dialog: katzen.PendingVouchersDialog) -> int:
        dialog.list_widget.setCurrentRow(0)
        dialog._cancel_selected()
        return 1

    monkeypatch.setattr(katzen, "list_pending_vouchers", listing)
    monkeypatch.setattr(katzen, "cancel_pending_voucher", cancel)
    monkeypatch.setattr(katzen, "_dialog_finished", close_it)
    await window.show_pending_vouchers()
    assert cancelled == [pv_id]


@pytest.mark.asyncio
async def test_main_populates_the_window_and_starts_the_listeners(
    window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        window,
        "_supervised_listener",
        appending_from(started, first_argument),
    )
    monkeypatch.setattr(katzen, "pending_joiner_join_conversation_ids", list)
    seeded = await seed_conversation(name="startup room")
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(
            persistent.AppSetting(
                id="chat.font.family",
                type="str",
                value="Serif",
            )
        )
        sess.add(
            persistent.AppSetting(
                id="chat.font.pointSize",
                type="int",
                value="13",
            )
        )
        sess.add(persistent.AppSetting(id="junk", type="bool", value="x"))
        sess.commit()

    await katzen.main(window)
    for _ in range(80):
        await asyncio.sleep(0)

    assert window.settings["chat.font.family"] == "Serif"
    assert window.settings["chat.font.pointSize"] == 13
    assert window.settings["junk"] is None
    assert seeded.conversation_id in window.conversation_state_by_id
    assert started == [
        "receive_msg_listener",
        "peer_added_listener",
        "tally_listener",
        "transfers_listener",
    ]
    assert window.isVisible() is True
    assert window.echomix_icon.isNull() is False


@pytest.mark.asyncio
async def test_main_installs_an_exception_handler_that_reports(
    window: katzen.MainWindow,
    boxes: type[FakeMessageBox],
    instant_timer: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        window,
        "_supervised_listener",
        ignore,
    )
    monkeypatch.setattr(katzen, "pending_joiner_join_conversation_ids", list)
    await katzen.main(window)
    asyncio.get_running_loop().call_exception_handler({"message": "boom"})
    assert boxes.seen[-1].kind == "critical"
    assert boxes.seen[-1].title == "Exception"


@pytest.mark.asyncio
async def test_a_pending_join_is_resumed_for_a_known_conversation(
    loaded_window: katzen.MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[str] = []
    convo_id = loaded_window.convo_state().conversation_id
    monkeypatch.setattr(
        loaded_window,
        "_supervised_listener",
        appending_from(resumed, first_argument),
    )
    monkeypatch.setattr(
        katzen,
        "pending_joiner_join_conversation_ids",
        returning([convo_id, 4242]),
    )
    await katzen._resume_pending_joins(loaded_window)
    assert resumed == [f"_await_voucher_join:{convo_id}"]


@pytest.mark.asyncio
async def test_a_supervised_listener_restarts_after_a_crash(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs: list[int] = []

    async def flaky() -> None:
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("listener died")

    caplog.set_level(logging.ERROR, logger="katzen")
    window._supervised_listener("flaky", flaky, backoff_s=0.0)
    for _ in range(40):
        await asyncio.sleep(0)
    assert len(runs) == 2
    assert "flaky: died with listener died; restarting in 0.0s" in caplog.text


@pytest.mark.asyncio
async def test_a_supervised_listener_may_stop_quietly(
    window: katzen.MainWindow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs: list[int] = []

    async def once() -> None:
        runs.append(1)

    caplog.set_level(logging.ERROR, logger="katzen")
    window._supervised_listener("once", once, backoff_s=0.0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert runs == [1]
    assert "once:" not in caplog.text


@pytest.mark.asyncio
async def test_a_cancelled_listener_is_not_restarted(
    window: katzen.MainWindow,
) -> None:
    started: list[int] = []

    async def forever() -> None:
        started.append(1)
        await asyncio.Event().wait()

    window._supervised_listener("forever", forever, restart_on_finish=True)
    for _ in range(5):
        await asyncio.sleep(0)
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert started == [1]
