"""Coverage for ConversationLogModel.data()'s attachment-role dispatch: a
persisted ConversationLog row is exposed to QML through the added roles."""
import os
import uuid
from collections.abc import Iterator

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

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
    app = QGuiApplication.instance() or QGuiApplication([])
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
