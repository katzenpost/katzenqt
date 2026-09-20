"""Coverage for ConversationLogModel.data()'s attachment-role dispatch: a
persisted ConversationLog row is exposed to QML through the added roles."""
import os
import uuid
from collections.abc import Iterator

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
# QApplication (not QGuiApplication): test_qt_tally builds QWidgets, and only
# one Qt application instance may exist per process, so every module must agree
# on the widest base class. QApplication is a strict superset, so the
# model-only tests behave identically under it.
from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import persistent  # noqa: E402
from katzenqt.qt_models import (  # noqa: E402
    ROLE_CHAT_ATTACHMENT_BASENAME,
    ROLE_CHAT_ATTACHMENT_KIND,
    ROLE_CHAT_IS_AUDIO_MESSAGE,
    ROLE_CHAT_MESSAGE_ID,
    ROLE_CHAT_PICTURE_PATH,
    ConversationLogModel,
)


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


def test_data_exposes_attachment_roles_from_a_persisted_row() -> None:
    mid = uuid.uuid4()
    payload = b"F" + cbor2.dumps({
        "v": 0, "kind": "file_marker", "basename": "note.opus",
        "filetype": "audio/opus", "size": 1,
        "rel_path": "attachments/900/x.opus",
        "sha256": b"\x00" * 32, "membership_hash": b"m",
    })
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            id=mid, conversation_id=900, conversation_peer_id=1,
            conversation_order=0, payload=payload,
        ))
        sess.commit()

    model = ConversationLogModel(convo_id=900)
    # The added roles are exposed to QML by name.
    assert model.roleNames()[ROLE_CHAT_ATTACHMENT_BASENAME] == b"attachment_basename"
    index = model.createIndex(0, 0)

    assert model.data(index, 0) == "Voice note: note.opus"
    assert model.data(index, ROLE_CHAT_ATTACHMENT_BASENAME) == "note.opus"
    assert model.data(index, ROLE_CHAT_IS_AUDIO_MESSAGE) is True
    assert model.data(index, ROLE_CHAT_ATTACHMENT_KIND) == "marker"
    assert model.data(index, ROLE_CHAT_PICTURE_PATH) is None
    assert model.data(index, ROLE_CHAT_MESSAGE_ID) == str(mid)
    # A role outside the exposed set short-circuits to None.
    assert model.data(index, 0xDEAD) is None


def _append_row(convo_id: int, order: int, peer_id: int = 1) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.ConversationLog(
            conversation_id=convo_id, conversation_peer_id=peer_id,
            conversation_order=order, payload=b"F" + cbor2.dumps({"v": 0, "text": "x"}),
        ))
        sess.commit()


def test_row_count_is_read_from_the_log_on_refresh() -> None:
    """rowCount reflects the DB, not a hand-maintained counter."""
    convo_id = 1234
    for order in range(3):
        _append_row(convo_id, order)
    model = ConversationLogModel(convo_id=convo_id)

    assert model.rowCount(None) == 0  # cache starts empty
    model.refresh_row_count()
    assert model.rowCount(None) == 3


def test_a_writer_that_forgets_to_notify_still_shows_its_row() -> None:
    """Regression for the tally/local-send bug: a caller that appends a log
    row without any notification must not desync the view permanently. The
    next refresh re-reads the truth from the log."""
    convo_id = 1235
    _append_row(convo_id, 0)
    model = ConversationLogModel(convo_id=convo_id)
    model.refresh_row_count()
    assert model.rowCount(None) == 1

    # A writer appends a row but does NOT notify the model.
    _append_row(convo_id, 1)
    assert model.rowCount(None) == 1  # cache unchanged, as expected

    # The next notification (any conversation update) self-heals.
    grown: "list[tuple[int, int]]" = []
    model.rowsInserted.connect(
        lambda _p, first, last: grown.append((first, last))
    )
    model.refresh_row_count()
    assert model.rowCount(None) == 2
    assert grown == [(1, 1)]


def test_row_count_shrink_resets_and_clears_caches() -> None:
    """A future deletion shrinks the count; the model resets."""
    convo_id = 1236
    for order in range(3):
        _append_row(convo_id, order)
    model = ConversationLogModel(convo_id=convo_id)
    model.refresh_row_count()
    assert model.rowCount(None) == 3
    index = model.index(0, 0, None)
    assert model.data(index, ROLE_CHAT_MESSAGE_ID) is not None  # warm the cache

    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.exec(
            persistent.select(persistent.ConversationLog).where(
                persistent.ConversationLog.conversation_id == convo_id,
                persistent.ConversationLog.conversation_order == 2,
            )
        ).one()
        sess.delete(row)
        sess.commit()

    resets: "list[bool]" = []
    model.modelReset.connect(lambda: resets.append(True))
    model.refresh_row_count()
    assert model.rowCount(None) == 2
    assert resets == [True]
