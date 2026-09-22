"""Coverage for the Transfers panel's DownloadsModel:
row management (start/piece/complete/pause), role exposure, and the
startup seeding from persistent ReadCapWAL rows."""
import os
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QModelIndex,
    QCoreApplication,
    Qt,
)
from PySide6.QtGui import QGuiApplication  # noqa: E402

from katzenqt import network, persistent, qt_models  # noqa: E402
from katzenqt.qt_models import (  # noqa: E402
    ROLE_TRANSFER_ACTIVE,
    ROLE_TRANSFER_CONV_ID,
    ROLE_TRANSFER_DIRECTION,
    ROLE_TRANSFER_FAILED,
    ROLE_TRANSFER_FAILURE_REASON,
    ROLE_TRANSFER_PARENT_NAME,
    ROLE_TRANSFER_PIECES,
    ROLE_TRANSFER_RATE,
    ROLE_TRANSFER_RCW_ID,
    ROLE_TRANSFER_TOTAL,
    DownloadsModel,
)


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def test_start_transfer_inserts_a_row():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    assert model.rowCount() == 0
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=3)
    assert model.rowCount() == 1
    idx = model.index(0, 0)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "alice"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Downloading"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "0/3"
    assert model.data(idx, ROLE_TRANSFER_RCW_ID) == str(rcw_id)
    assert model.data(idx, ROLE_TRANSFER_CONV_ID) == 7
    assert model.data(idx, ROLE_TRANSFER_PARENT_NAME) == "alice"
    assert model.data(idx, ROLE_TRANSFER_TOTAL) == 3
    assert model.data(idx, ROLE_TRANSFER_PIECES) == 0
    assert model.data(idx, ROLE_TRANSFER_ACTIVE) is True


def test_start_transfer_refreshes_denominator_when_total_becomes_known():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=None)
    assert model.rowCount() == 1
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "0 pieces"
    # A later "started" with a known total keeps a single row.
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=5)
    assert model.rowCount() == 1
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "0/5"


def test_notify_piece_updates_progress():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=4)
    model.notify_piece(rcw_id, pieces=2)
    idx = model.index(0, 1)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "2/4"
    assert model.data(idx, ROLE_TRANSFER_PIECES) == 2


def test_notify_piece_unknown_row_is_ignored():
    model = DownloadsModel()
    model.notify_piece(uuid.uuid4(), pieces=1)  # must not raise
    assert model.rowCount() == 0


def test_complete_transfer_removes_the_row():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=2)
    model.notify_piece(rcw_id, pieces=2)
    model.complete_transfer(rcw_id)
    assert model.rowCount() == 0
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) is None


def test_set_paused_toggles_state_column():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=2)
    model.set_paused(rcw_id, paused=True)
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Paused"
    assert model.data(model.index(0, 0), ROLE_TRANSFER_ACTIVE) is False
    model.set_paused(rcw_id, paused=False)
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Downloading"
    assert model.data(model.index(0, 0), ROLE_TRANSFER_ACTIVE) is True


def test_set_paused_announces_display_role():
    """Pausing changes the State text as well as the active flag, so the
    dataChanged roles must include DisplayRole or the view keeps the old
    'Uploading'/'Downloading' label."""
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=2)
    seen: "list[list[int]]" = []
    model.dataChanged.connect(lambda _tl, _br, roles: seen.append(list(roles)))
    model.set_paused(rcw_id, paused=True)
    assert Qt.ItemDataRole.DisplayRole in seen[-1]
    assert ROLE_TRANSFER_ACTIVE in seen[-1]


def test_fail_transfer_keeps_row_visible_with_reason():
    """A failed transfer stays visible in the model with state
    'Failed: {reason}' so the user can see what went wrong."""
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=5)
    
    model.fail_transfer(rcw_id, "MalformedChunkError: invalid CRC")
    
    assert rcw_id in model._rows
    assert model._rows[rcw_id]["failed"] is True
    assert model._rows[rcw_id]["failure_reason"] == "MalformedChunkError: invalid CRC"
    assert model._rows[rcw_id]["active"] is False
    # State column shows the failure reason
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Failed: MalformedChunkError: invalid CRC"
    # Roles expose the failed flag and reason
    assert model.data(model.index(0, 0), ROLE_TRANSFER_FAILED) is True
    assert model.data(model.index(0, 0), ROLE_TRANSFER_FAILURE_REASON) == "MalformedChunkError: invalid CRC"


def test_fail_transfer_unknown_row_is_ignored():
    """Calling fail_transfer on a non-existent row must not raise."""
    model = DownloadsModel()
    model.fail_transfer(uuid.uuid4(), "some error")  # must not raise
    assert model.rowCount() == 0


def test_remove_transfer_deletes_row():
    """Remove a transfer row from the model (user-dismissal of failed/complete)."""
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=2)
    model.fail_transfer(rcw_id, "test error")
    assert model.rowCount() == 1
    
    model.remove_transfer(rcw_id)
    
    assert rcw_id not in model._rows
    assert rcw_id not in model._order
    assert model.rowCount() == 0


def test_remove_transfer_unknown_row_is_ignored():
    """Calling remove_transfer on a non-existent row must not raise."""
    model = DownloadsModel()
    model.remove_transfer(uuid.uuid4())  # must not raise
    assert model.rowCount() == 0


def test_column_and_role_metadata():
    model = DownloadsModel()
    assert model.columnCount() == 4
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Contact"
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Progress"
    assert model.headerData(2, Qt.Orientation.Horizontal) == "State"
    assert model.headerData(3, Qt.Orientation.Horizontal) == "Rate"
    names = model.roleNames()
    assert names[ROLE_TRANSFER_RCW_ID] == b"transfer_rcw_id"
    assert names[ROLE_TRANSFER_TOTAL] == b"transfer_total"
    assert names[ROLE_TRANSFER_RATE] == b"transfer_rate"
    assert model.data(QModelIndex(), Qt.ItemDataRole.DisplayRole) is None
    assert model.data(model.index(5, 0), Qt.ItemDataRole.DisplayRole) is None


@pytest.mark.asyncio
async def test_seed_from_db_lists_active_and_partial_transfers():
    """A resumable substream (active peer, or paused with received pieces)
    shows up as a Transfers row named after its parent conversation peer;
    a fully-completed substream (inactive, zero pieces) does not."""
    owner_wcw = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
    )
    owner_id = owner_wcw.id
    owner_rcw = persistent.ReadCapWAL(
        id=owner_id, write_cap_id=owner_id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    async with persistent.asession() as sess:
        conv = persistent.Conversation(
            name="carol-conv", write_cap=owner_wcw.id, first_unread=0,
        )
        own_peer = persistent.ConversationPeer(
            name="me", read_cap_id=owner_rcw.id, active=False,
            conversation=conv,
        )
        conv.own_peer = own_peer
        sess.add(owner_wcw)
        sess.add(owner_rcw)
        sess.add(conv)
        await sess.flush()
        conv_id = conv.id
        await sess.commit()

        parent = persistent.ConversationPeer(
            name="carol",
            read_cap_id=owner_id,
            active=True,
            conversation=conv,
        )
        sess.add(parent)
        await sess.flush()
        parent_id = parent.id
        await sess.commit()

        active_rcw_id = uuid.uuid4()
        paused_rcw_id = uuid.uuid4()
        done_rcw_id = uuid.uuid4()
        sess.add_all((
            persistent.ReadCapWAL(
                id=active_rcw_id, read_cap=b"\x11" * 136, next_index=b"\x22" * 104,
                substream_total_chunks=2,
            ),
            persistent.ReadCapWAL(
                id=paused_rcw_id, read_cap=b"\x33" * 136, next_index=b"\x44" * 104,
            ),
            persistent.ReadCapWAL(
                id=done_rcw_id, read_cap=b"\x55" * 136, next_index=b"\x66" * 104,
            ),
            persistent.ConversationPeer(
                name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:aa",
                read_cap_id=active_rcw_id,
                active=True,
                conversation=conv,
            ),
            persistent.ConversationPeer(
                name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:bb",
                read_cap_id=paused_rcw_id,
                active=False,
                conversation=conv,
            ),
            persistent.ConversationPeer(
                name=f"{network._SUBSTREAM_NAME_PREFIX}{parent_id}:cc",
                read_cap_id=done_rcw_id,
                active=False,
                conversation=conv,
            ),
        ))
        sess.add(persistent.ReceivedPiece(
            read_cap=paused_rcw_id,
            bacap_index=b"\x00" * 8,
            chunk_type=b"C",
            chunk=b"x",
        ))
        await sess.commit()
    assert conv_id is not None
    assert parent_id is not None

    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 2
    rcw_ids = {
        str(model.data(model.index(r, 0), ROLE_TRANSFER_RCW_ID))
        for r in range(model.rowCount())
    }
    assert rcw_ids == {str(active_rcw_id), str(paused_rcw_id)}
    # The parent (non-substream) name is shown, and paused is flagged.
    for r in range(model.rowCount()):
        row_id = str(model.data(model.index(r, 0), ROLE_TRANSFER_RCW_ID))
        assert model.data(model.index(r, 0), ROLE_TRANSFER_PARENT_NAME) == "carol"
        assert not str(
            model.data(model.index(r, 0), ROLE_TRANSFER_PARENT_NAME)
        ).startswith(network._SUBSTREAM_NAME_PREFIX)
        if row_id == str(active_rcw_id):
            assert model.data(model.index(r, 0), ROLE_TRANSFER_ACTIVE) is True
            assert model.data(model.index(r, 1), ROLE_TRANSFER_TOTAL) == 2
        else:
            assert model.data(model.index(r, 0), ROLE_TRANSFER_ACTIVE) is False
            assert model.data(model.index(r, 1), ROLE_TRANSFER_PIECES) == 1


def test_seed_from_db_uses_only_the_sync_engine(monkeypatch):
    """seed_from_db runs on the Qt loop and must never open the async engine:
    the two loops sharing it is what produced the "Lock is bound to a
    different event loop" startup crash. Fail loudly if asession is touched."""
    async def _boom():
        raise AssertionError("seed_from_db opened the async engine")
        yield  # pragma: no cover

    monkeypatch.setattr(persistent, "asession", _boom)
    model = DownloadsModel()
    model.seed_from_db()  # must complete on the sync engine alone
    assert model.rowCount() == 0


def test_direction_defaults_to_download_and_drives_state_text():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=2)
    assert model.data(model.index(0, 0), ROLE_TRANSFER_DIRECTION) == "download"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Downloading"
    model.set_paused(rcw_id, paused=True)
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Paused"


def test_upload_direction_renders_uploading_state():
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(
        rcw_id, conversation_id=7, parent_name="carol", total=4,
        direction="upload",
    )
    assert model.data(model.index(0, 0), ROLE_TRANSFER_DIRECTION) == "upload"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Uploading"
    model.notify_piece(rcw_id, pieces=2)
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "2/4"
    model.set_paused(rcw_id, paused=True)
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Paused"


async def _make_conversation_with_upload(
    *, remaining_chunks: int, total_chunks: int = 3, paused: bool = False,
) -> tuple[int, uuid.UUID]:
    """Build a conversation plus an in-flight outbound substream.

    Returns ``(conversation_id, indirection_rcw_id)``. The I-chunk on the main
    stream carries ``indirection``; ``remaining_chunks`` agg-stream C/F PWAL
    rows are still outstanding.
    """
    owner_wcw = persistent.WriteCapWAL(
        id=uuid.uuid4(), write_cap=b"\x00" * 168, next_index=b"\x00" * 104,
    )
    owner_rcw = persistent.ReadCapWAL(
        id=owner_wcw.id, write_cap_id=owner_wcw.id,
        read_cap=b"\x00" * 136, next_index=b"\x00" * 104,
    )
    agg = uuid.uuid4()
    indirection_rcw_id = uuid.uuid4()
    async with persistent.asession() as sess:
        conv = persistent.Conversation(
            name="carol-conv", write_cap=owner_wcw.id, first_unread=0,
        )
        own_peer = persistent.ConversationPeer(
            name="me", read_cap_id=owner_rcw.id, active=False,
            conversation=conv,
        )
        conv.own_peer = own_peer
        sess.add(owner_wcw)
        sess.add(owner_rcw)
        sess.add(conv)
        await sess.flush()
        conv_id = conv.id
        sess.add_all((
            persistent.WriteCapWAL(
                id=agg, write_cap=b"\x01" * 168, next_index=b"\x00" * 104,
                paused=paused,
            ),
            persistent.ReadCapWAL(
                id=indirection_rcw_id, write_cap_id=agg,
                read_cap=b"\x11" * 136, next_index=b"\x22" * 104,
                substream_total_chunks=total_chunks,
            ),
            persistent.PlaintextWAL(
                id=uuid.uuid4(), bacap_stream=owner_wcw.id,
                conversation_id=conv_id, bacap_payload=b"",
                indirection=indirection_rcw_id,
            ),
        ))
        for _ in range(remaining_chunks):
            sess.add(persistent.PlaintextWAL(
                id=uuid.uuid4(), bacap_stream=agg,
                conversation_id=conv_id, bacap_payload=b"Cchunk",
            ))
        await sess.commit()
    return conv_id, indirection_rcw_id


@pytest.mark.asyncio
async def test_seed_from_db_lists_in_flight_upload():
    conv_id, rcw_id = await _make_conversation_with_upload(
        remaining_chunks=1, total_chunks=3,
    )
    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), ROLE_TRANSFER_RCW_ID) == str(rcw_id)
    assert model.data(model.index(0, 0), ROLE_TRANSFER_DIRECTION) == "upload"
    assert model.data(model.index(0, 0), ROLE_TRANSFER_PARENT_NAME) == "carol-conv"
    assert model.data(model.index(0, 0), ROLE_TRANSFER_CONV_ID) == conv_id
    # 3 total chunks, 1 outstanding -> 2 sent.
    assert model.data(model.index(0, 1), ROLE_TRANSFER_PIECES) == 2
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Uploading"


@pytest.mark.asyncio
async def test_seed_from_db_skips_completed_upload():
    """A substream with no remaining C/F PWALs has finished uploading (the
    gated I-chunk is now dispatchable), so it gets no Transfers row; the chat
    bubble still shows pending until the I-chunk is ACK'd."""
    await _make_conversation_with_upload(remaining_chunks=0, total_chunks=3)
    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 0


@pytest.mark.asyncio
async def test_seed_from_db_marks_a_paused_upload_paused():
    await _make_conversation_with_upload(
        remaining_chunks=2, total_chunks=3, paused=True,
    )
    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), ROLE_TRANSFER_DIRECTION) == "upload"
    assert model.data(model.index(0, 0), ROLE_TRANSFER_ACTIVE) is False
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Paused"

class _Clock:
    """Stand-in for the ``time`` module so rate tests control the clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t


def _rate(model: DownloadsModel) -> object:
    return model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole)


def test_rate_is_a_placeholder_before_one_second(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=10)
    assert _rate(model) == "—"
    clock.t += 0.5
    model.notify_piece(rcw_id, 1, raw_bytes=100)
    assert _rate(model) == "—"


def test_rate_reports_average_bytes_since_start(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=10)
    clock.t += 2.0
    model.notify_piece(rcw_id, 4, raw_bytes=4096)
    assert _rate(model) == "2.0 KiB/s"


def test_rate_is_zero_while_paused_and_resets_on_unpause(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=10)
    clock.t += 2.0
    model.notify_piece(rcw_id, 4, raw_bytes=4096)
    assert _rate(model) == "2.0 KiB/s"

    model.set_paused(rcw_id, paused=True)
    assert _rate(model) == "0 B/s"

    # A long pause must not count toward the next average.
    clock.t += 100.0
    model.set_paused(rcw_id, paused=False)
    assert _rate(model) == "—"  # interval reset; no time elapsed yet
    clock.t += 2.0
    model.notify_piece(rcw_id, 6, raw_bytes=8192)
    assert _rate(model) == "2.0 KiB/s"  # (8192-4096)/2


def test_upload_rate_counts_sent_bytes_from_the_remaining_total(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(
        rcw_id, conversation_id=7, parent_name="bob", total=10,
        direction="upload", raw_bytes=10000,
    )
    clock.t += 2.0
    model.notify_piece(rcw_id, 4, raw_bytes=6000)  # 4000 sent
    assert _rate(model) == "2.0 KiB/s"  # 2000 B/s


def test_failed_transfer_rate_is_zero(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    model = DownloadsModel()
    rcw_id = uuid.uuid4()
    model.start_transfer(rcw_id, conversation_id=7, parent_name="alice", total=10)
    clock.t += 2.0
    model.notify_piece(rcw_id, 4, raw_bytes=4096)
    model.fail_transfer(rcw_id, "boom")
    assert _rate(model) == "0 B/s"


@pytest.mark.asyncio
async def test_seed_from_db_upload_rate_starts_at_zero(monkeypatch):
    """Seeding sets the rate baseline to the bytes still on disk, so a resumed
    transfer's rate counts only bytes sent after the relaunch."""
    clock = _Clock()
    monkeypatch.setattr(qt_models, "time", clock)
    await _make_conversation_with_upload(remaining_chunks=2, total_chunks=3)
    model = DownloadsModel()
    model.seed_from_db()
    assert model.rowCount() == 1
    clock.t += 2.0
    # No chunk ACK'd since seed, so no bytes transferred in the interval.
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "0 B/s"
