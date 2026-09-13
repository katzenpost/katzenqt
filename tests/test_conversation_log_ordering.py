"""The Qt model's display row is decoupled from the stored conversation_order:
the default strategy is the identity map (behaviour unchanged), and an opt-in
strategy permutes the rows data() serves without touching storage."""
import os
import uuid
from collections.abc import Iterator

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from katzenqt import persistent  # noqa: E402
from katzenqt.qt_models import ROLE_CHAT_MESSAGE_ID, ConversationLogModel  # noqa: E402

_CONVO = 950


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def _seed_rows() -> list[str]:
    """Four rows, conversation_order 0..3, sender epochs e0,e1,e0,e1. Returns
    the message ids indexed by conversation_order."""
    e0, e1 = b"\xa0" * 32, b"\xb1" * 32
    epochs = [e0, e1, e0, e1]
    ids: list[str] = []
    with persistent.Session(persistent._engine_sync) as sess:
        for order, epoch in enumerate(epochs):
            mid = uuid.uuid4()
            ids.append(str(mid))
            payload = b"F" + cbor2.dumps({"membership_hash": epoch})
            sess.add(persistent.ConversationLog(
                id=mid, conversation_id=_CONVO, conversation_peer_id=1,
                conversation_order=order, payload=payload,
            ))
        sess.commit()
    return ids


def _row_ids(model: ConversationLogModel, n: int) -> list[str]:
    return [model.data(model.createIndex(i, 0), ROLE_CHAT_MESSAGE_ID)
            for i in range(n)]


def test_default_insertion_is_identity(monkeypatch) -> None:
    monkeypatch.delenv("KQT_ORDERING", raising=False)
    ids = _seed_rows()
    model = ConversationLogModel(convo_id=_CONVO)
    model.row_count = 4
    assert _row_ids(model, 4) == ids


def test_epoch_strategy_permutes_served_rows(monkeypatch) -> None:
    monkeypatch.setenv("KQT_ORDERING", "epoch")
    ids = _seed_rows()
    model = ConversationLogModel(convo_id=_CONVO)
    model.row_count = 4
    assert _row_ids(model, 4) == [ids[0], ids[2], ids[1], ids[3]]
    with persistent.Session(persistent._engine_sync) as sess:
        stored = sorted(r.conversation_order for r in sess.query(
            persistent.ConversationLog).filter(
            persistent.ConversationLog.conversation_id == _CONVO).all())
    assert stored == [0, 1, 2, 3]
