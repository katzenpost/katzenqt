from __future__ import annotations

import logging
import os
import uuid
from types import SimpleNamespace
from typing import cast

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import (  # noqa: E402
    grouphash, katzen, models, persistent, qt_models,
)
from tests.test_katzen_gui_common import (  # noqa: E402,F401
    boxes,
    qt_app,
    window,
)

_NEXT_CONVO = [971]
OWN_WRITE_CAP = bytes((i % 251) for i in range(168))
OWN_CAP = OWN_WRITE_CAP[32:]
ALICE_CAP = bytes([7]) * 136
SELF_ONLY = models.canonical_membership_hash([OWN_CAP])
PAIR = models.canonical_membership_hash([OWN_CAP, ALICE_CAP])


@pytest.fixture(autouse=True)
def _app(qt_app: "QApplication") -> None:
    return None


def _text(claim: "bytes | None") -> bytes:
    fields: "dict[str, object]" = {"version": 0, "msg_type": 0, "text": "hi"}
    if claim is not None:
        fields["membership_hash"] = claim
    return b"F" + cbor2.dumps(fields)


def _intro(cap: bytes, claim: bytes) -> bytes:
    return b"F" + cbor2.dumps({
        "version": 0,
        "membership_hash": claim,
        "msg_type": 1,
        "introduction": {"display_name": "alice", "read_cap": cap},
    })


def _seed(rows: "list[tuple[int, bytes, bool]]") -> int:
    convo = _NEXT_CONVO[0]
    _NEXT_CONVO[0] += 1
    own_peer, alice = convo * 10, convo * 10 + 1
    rc_own, rc_a, wc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.WriteCapWAL(id=wc, write_cap=OWN_WRITE_CAP))
        sess.add(persistent.ReadCapWAL(id=rc_own, read_cap=bytes([1]) * 136))
        sess.add(persistent.ReadCapWAL(id=rc_a, read_cap=ALICE_CAP))
        sess.add(
            persistent.ConversationPeer(
                id=own_peer, name="me", read_cap_id=rc_own,
            ),
        )
        sess.add(
            persistent.ConversationPeer(
                id=alice, name="alice", read_cap_id=rc_a,
            ),
        )
        sess.add(persistent.Conversation(
            id=convo, name="c", own_peer_id=own_peer, write_cap=wc,
        ))
        sess.add(persistent.ConversationPeerLink(
            conversation_id=convo, conversation_peer_id=alice,
        ))
        for order, payload, mine in rows:
            sess.add(persistent.ConversationLog(
                id=uuid.uuid4(),
                conversation_id=convo,
                conversation_peer_id=own_peer if mine else alice,
                conversation_order=order,
                payload=payload,
            ))
        sess.commit()
    return convo


def _model(convo: int) -> qt_models.ConversationLogModel:
    model = qt_models.ConversationLogModel(convo)
    model.refresh_row_count()
    return model


def _role(
    model: qt_models.ConversationLogModel, row: int, role: int,
) -> object:
    return model.data(model.index(row, 0), role)


def test_the_tint_is_off_until_it_is_turned_on() -> None:
    convo = _seed([(0, _text(PAIR), True)])
    model = _model(convo)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == ""
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_BOUNDARY) is False
    model.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == (
        grouphash.color_for(PAIR)
    )


def test_the_colour_changes_where_a_member_is_announced() -> None:
    convo = _seed([
        (0, _text(SELF_ONLY), True),
        (1, _intro(ALICE_CAP, PAIR), False),
        (2, _text(PAIR), False),
    ])
    model = _model(convo)
    model.set_group_tint(True)
    colors = [
        _role(model, r, qt_models.ROLE_CHAT_GROUP_COLOR) for r in range(3)
    ]
    assert colors[0] == grouphash.color_for(SELF_ONLY)
    assert colors[1] == colors[2] == grouphash.color_for(PAIR)
    assert [
        _role(model, r, qt_models.ROLE_CHAT_GROUP_BOUNDARY) for r in range(3)
    ] == [False, True, False]


def test_a_peer_claiming_another_membership_is_loud() -> None:
    convo = _seed([
        (0, _text(b"TODO" * 8), False), (1, _text(PAIR), False),
    ])
    model = _model(convo)
    model.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == (
        grouphash.SUSPECT_COLOR
    )
    assert _role(model, 1, qt_models.ROLE_CHAT_GROUP_COLOR) == (
        grouphash.color_for(PAIR)
    )


def test_our_own_upload_marker_claims_nothing_and_is_not_loud() -> None:
    marker = b"F" + cbor2.dumps({
        "kind": "file_outgoing", "basename": "cat.png",
    })
    convo = _seed([(0, marker, True), (1, _text(PAIR), True)])
    model = _model(convo)
    model.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == (
        grouphash.color_for(PAIR)
    )


def test_turning_the_tint_off_again_clears_the_colour() -> None:
    convo = _seed([(0, _text(PAIR), True)])
    model = _model(convo)
    model.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) != ""
    model.set_group_tint(False)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == ""


def test_setting_the_same_value_twice_does_nothing() -> None:
    convo = _seed([(0, _text(PAIR), True)])
    model = _model(convo)
    model.set_group_tint(False)
    model.set_group_tint(True)
    model.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) != ""


def test_a_conversation_that_is_gone_has_no_rows() -> None:
    assert qt_models._arrival_membership_rows(999999) == []

def test_an_inactive_or_substream_peer_is_not_a_member() -> None:
    convo = _seed([(0, _text(PAIR), True)])
    with persistent.Session(persistent._engine_sync) as sess:
        rc_sub, rc_off = uuid.uuid4(), uuid.uuid4()
        sess.add(persistent.ReadCapWAL(id=rc_sub, read_cap=bytes([9]) * 136))
        sess.add(persistent.ReadCapWAL(id=rc_off, read_cap=bytes([8]) * 136))
        sub = persistent.ConversationPeer(
            name=f"{models.SUBSTREAM_NAME_PREFIX}file",
            read_cap_id=rc_sub,
        )
        off = persistent.ConversationPeer(
            name="departed", read_cap_id=rc_off, active=False,
        )
        sess.add(sub)
        sess.add(off)
        sess.commit()
        for peer in (sub, off):
            sess.add(persistent.ConversationPeerLink(
                conversation_id=convo, conversation_peer_id=peer.id,
            ))
        sess.commit()
    rows = qt_models._arrival_membership_rows(convo)
    assert [local for _order, local, _suspect in rows] == [PAIR]


def test_a_view_nobody_has_asked_about_repaints_nothing() -> None:
    convo = _seed([(0, _text(PAIR), True)])
    model = qt_models.ConversationLogModel(convo)
    painted: "list[object]" = []
    model.dataChanged.connect(lambda *a: painted.append(a))
    model.set_group_tint(True)
    assert painted == []


def test_the_menu_action_persists_the_choice(
    window: "katzen.MainWindow",
) -> None:
    assert window.group_tint_action.isChecked() is False
    window.group_tint_action.setChecked(True)
    assert window.settings[katzen.GROUP_TINT_SETTING] == 1
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, katzen.GROUP_TINT_SETTING)
        assert row is not None and row.value == "1"
    window.group_tint_action.setChecked(False)
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, katzen.GROUP_TINT_SETTING)
        assert row is not None and row.value == "0"


def test_a_persisted_choice_comes_back_on_restore(
    window: "katzen.MainWindow",
) -> None:
    window.settings = {katzen.GROUP_TINT_SETTING: 1}
    window.restore_group_tint()
    assert window.group_tint_action.isChecked() is True
    window.settings = {}
    window.restore_group_tint()
    assert window.group_tint_action.isChecked() is False


def test_a_database_that_will_not_take_the_setting_is_only_logged(
    window: "katzen.MainWindow",
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_session(*_a: object, **_k: object) -> object:
        raise RuntimeError("no database today")

    monkeypatch.setattr(persistent, "Session", no_session)
    with caplog.at_level(logging.WARNING, logger=katzen.logger.name):
        window.set_group_tint(True)
    assert any(
        "could not persist the membership tint" in r.message
        for r in caplog.records
    )
    assert window.settings[katzen.GROUP_TINT_SETTING] == 1


def test_every_open_conversation_follows_the_choice(
    window: "katzen.MainWindow",
) -> None:
    convo = _seed([(0, _text(PAIR), True)])
    model = _model(convo)
    window.conversation_state_by_id[convo] = cast(
        "katzen.ConversationUIState",
        SimpleNamespace(conversation_log_model=model),
    )
    window.set_group_tint(True)
    assert _role(model, 0, qt_models.ROLE_CHAT_GROUP_COLOR) == (
        grouphash.color_for(PAIR)
    )
