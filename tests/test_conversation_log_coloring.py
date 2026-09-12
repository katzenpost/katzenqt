"""Inline arrival-epoch colouring: each chat row is tinted by the LOCAL
membership hash it arrived in (reconstructed by replaying INTRODUCTIONs), and a
row where that hash changes from the row above is flagged as a striped-divider
boundary. Both are exposed to QML as data() roles."""
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
    ROLE_CHAT_EPOCH_BOUNDARY,
    ROLE_CHAT_EPOCH_COLOR,
    ConversationLogModel,
    arrival_membership_states,
)

_CONVO = 970


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> Iterator[QCoreApplication]:
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def _text(t: str) -> bytes:
    return b"F" + cbor2.dumps(
        {"version": 0, "membership_hash": bytes(32), "msg_type": 0, "text": t}
    )


def _intro(cap: bytes) -> bytes:
    return b"F" + cbor2.dumps({
        "version": 0, "membership_hash": bytes(32), "msg_type": 1,
        "introduction": {"display_name": "alice", "read_cap": cap},
    })


def _seed() -> list[str]:
    """A conversation that starts with just us, then alice is introduced at
    order 1 -> the arrival membership hash changes there. Returns message ids by
    conversation_order."""
    own_wc = bytes((i % 251) for i in range(168))
    cap_a = bytes([7]) * 136
    rc_own, rc_a, wc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids: list[str] = []
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.WriteCapWAL(id=wc, write_cap=own_wc))
        sess.add(persistent.ReadCapWAL(id=rc_own, read_cap=bytes([1]) * 136))
        sess.add(persistent.ReadCapWAL(id=rc_a, read_cap=cap_a))
        sess.add(persistent.ConversationPeer(id=1, name="me", read_cap_id=rc_own))
        sess.add(persistent.ConversationPeer(id=2, name="alice", read_cap_id=rc_a))
        sess.add(persistent.Conversation(
            id=_CONVO, name="c", own_peer_id=1, write_cap=wc))
        sess.add(persistent.ConversationPeerLink(
            conversation_id=_CONVO, conversation_peer_id=2))
        for order, payload in [
            (0, _text("hi")), (1, _intro(cap_a)), (2, _text("welcome"))
        ]:
            mid = uuid.uuid4()
            ids.append(str(mid))
            sess.add(persistent.ConversationLog(
                id=mid, conversation_id=_CONVO, conversation_peer_id=2,
                conversation_order=order, payload=payload,
            ))
        sess.commit()
    return ids


def test_arrival_states_change_only_where_membership_grows() -> None:
    ids = _seed()
    states = arrival_membership_states(_CONVO)
    assert states[ids[0]] != states[ids[1]]
    assert states[ids[1]] == states[ids[2]]


def test_later_deactivation_does_not_repaint_earlier_messages() -> None:
    """A peer deactivated after joining (e.g. a corrupt-chunk substream)
    must not retroactively change the arrival membership hash of messages
    that arrived while they were still active."""
    ids = _seed()
    before = dict(arrival_membership_states(_CONVO))

    with persistent.Session(persistent._engine_sync) as sess:
        peer = sess.get(persistent.ConversationPeer, 2)
        peer.active = False
        sess.add(peer)
        sess.commit()

    after = arrival_membership_states(_CONVO)
    assert after == before


def test_model_serves_epoch_color_and_boundary() -> None:
    ids = _seed()
    assert len(ids) == 3
    m = ConversationLogModel(convo_id=_CONVO)
    m.row_count = 3
    colors = [m.data(m.createIndex(i, 0), ROLE_CHAT_EPOCH_COLOR)
              for i in range(3)]
    assert all(c and c.startswith("#") for c in colors)
    assert colors[0] != colors[1]
    assert colors[1] == colors[2]
    bounds = [m.data(m.createIndex(i, 0), ROLE_CHAT_EPOCH_BOUNDARY)
              for i in range(3)]
    assert bounds == [False, True, False]
