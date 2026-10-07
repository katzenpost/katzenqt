from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from katzenqt import katzen, models, persistent  # noqa: E402

from tests.test_katzen_gui_common import (  # noqa: E402,F401
    FakeMessageBox,
    boxes,
    qt_app,
    window,
)

if TYPE_CHECKING:
    from katzenqt.katzen import MainWindow


def test_a_recorded_note_is_always_named_by_its_content() -> None:
    payload = b"an opus voice note"
    digest = hashlib.sha256(payload).hexdigest()
    for hash_all in (False, True):
        assert models.outgoing_basename(
            payload, "conversation-4-draft-1791374353468.opus",
            "audio/opus", hash_all=hash_all,
        ) == f"{digest}.opus"


def test_a_chosen_file_keeps_its_name_by_default() -> None:
    assert models.outgoing_basename(
        b"jpeg bytes", "holiday.jpg", "image/jpeg", hash_all=False,
    ) == "holiday.jpg"


def test_a_chosen_file_is_hashed_when_every_name_is() -> None:
    payload = b"jpeg bytes"
    digest = hashlib.sha256(payload).hexdigest()
    assert models.outgoing_basename(
        payload, "holiday.jpg", "image/jpeg", hash_all=True,
    ) == f"{digest}.jpg"


def test_a_suffix_is_lowercased_and_absence_is_kept() -> None:
    payload = b"x"
    digest = hashlib.sha256(payload).hexdigest()
    assert models.outgoing_basename(
        payload, "SHOUT.JPEG", "image/jpeg", hash_all=True,
    ) == f"{digest}.jpeg"
    assert models.outgoing_basename(
        payload, "notes", "text/plain", hash_all=True,
    ) == digest


def test_the_travelling_name_hides_what_the_capture_leaked() -> None:
    travelled = models.outgoing_basename(
        b"note", "conversation-4-draft-1791374353468.opus",
        "audio/opus", hash_all=False,
    )
    assert "conversation" not in travelled
    assert "1791374353468" not in travelled
    assert "draft" not in travelled


def test_the_menu_persists_the_choice(window: "MainWindow") -> None:
    actions = {str(a.data()): a for a in window.name_policy_group.actions()}
    actions[models.NAME_POLICY_HASH].trigger()
    assert window.settings[models.NAME_POLICY_SETTING] == (
        models.NAME_POLICY_HASH
    )
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, models.NAME_POLICY_SETTING)
        assert row is not None and row.value == models.NAME_POLICY_HASH
    actions[models.NAME_POLICY_KEEP].trigger()
    with persistent.Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, models.NAME_POLICY_SETTING)
        assert row is not None and row.value == models.NAME_POLICY_KEEP


def test_a_persisted_choice_comes_back_on_restore(
    window: "MainWindow",
) -> None:
    window.settings = {models.NAME_POLICY_SETTING: models.NAME_POLICY_HASH}
    window.restore_name_policy()
    checked = [a for a in window.name_policy_group.actions() if a.isChecked()]
    assert [str(a.data()) for a in checked] == [models.NAME_POLICY_HASH]
    window.settings = {}
    window.restore_name_policy()
    checked = [a for a in window.name_policy_group.actions() if a.isChecked()]
    assert [str(a.data()) for a in checked] == [models.NAME_POLICY_KEEP]


def test_a_database_that_will_not_take_the_setting_is_only_logged(
    window: "MainWindow",
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("no database today")

    monkeypatch.setattr(persistent, "Session", refuse)
    action = window.name_policy_group.actions()[1]
    with caplog.at_level(logging.WARNING, logger=katzen.logger.name):
        action.trigger()
    assert any(
        "could not persist the name policy" in r.message
        for r in caplog.records
    )
    assert window.settings[models.NAME_POLICY_SETTING] == str(action.data())
