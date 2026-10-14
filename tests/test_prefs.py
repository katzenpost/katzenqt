from __future__ import annotations

import logging

import pytest

from katzenqt import persistent, prefs, theme


def _row(key: str) -> "persistent.AppSetting | None":
    with persistent.Session(persistent._engine_sync) as sess:
        return sess.get(persistent.AppSetting, key)


def test_every_value_type_survives_encoding() -> None:
    for value in ["dark", 20, True, False, 1.5, ["x", "y"], {"k": 1}]:
        assert prefs._decode(*prefs._encode(value)) == value


def test_a_known_key_round_trips() -> None:
    prefs.put("theme.mode", "solarized_dark")
    assert prefs.get("theme.mode", None) == "solarized_dark"


def test_a_key_katzenqt_does_not_read_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=prefs.logger.name):
        prefs.put("theme.mdoe", "dark")
    assert _row(prefs.PREFIX + "theme.mdoe") is None
    assert any("theme.mdoe" in r.message for r in caplog.records)


def test_a_value_of_the_wrong_type_for_its_key_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prefs.put("theme.mode", "dark")
    with caplog.at_level(logging.WARNING, logger=prefs.logger.name):
        prefs.put("theme.mode", 3)
    assert prefs.get("theme.mode", None) == "dark"
    assert any("theme.mode" in r.message for r in caplog.records)


def test_an_unset_preference_is_the_default() -> None:
    assert prefs.get("never.set", "fallback") == "fallback"


def test_a_choice_already_recorded_without_the_prefix_is_honoured() -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.AppSetting(
            id="theme.mode", type="str", value="solarized_dark",
        ))
        sess.commit()
    assert prefs.get(
        "theme.mode", "system", legacy="theme.mode",
    ) == "solarized_dark"
    loaded = theme.ThemeManager._load_mode(None)  # type: ignore[arg-type]
    assert loaded == "solarized_dark"


def test_a_row_that_cannot_be_read_still_falls_back_to_the_legacy_key(
) -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.AppSetting(
            id=prefs.PREFIX + "old.key", type="sometype", value="x",
        ))
        sess.add(persistent.AppSetting(
            id="old.key", type="str", value="kept",
        ))
        sess.commit()
    assert prefs.get("old.key", "fallback", legacy="old.key") == "kept"


def test_an_unknown_stored_type_is_refused_rather_than_guessed() -> None:
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.AppSetting(
            id=prefs.PREFIX + "odd", type="sometype", value="x",
        ))
        sess.commit()
    with pytest.raises(ValueError, match="unknown setting type"):
        prefs._decode("sometype", "x")
    assert prefs.get("odd", "fallback") == "fallback"


def test_a_database_that_will_not_take_a_preference_is_only_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def no_session(*_a: object, **_k: object) -> object:
        raise RuntimeError("no database today")

    monkeypatch.setattr(persistent, "Session", no_session)
    with caplog.at_level(logging.WARNING, logger=prefs.logger.name):
        prefs.put("theme.mode", "dark")
    assert any(
        "could not persist preference pref.theme.mode" in r.message
        for r in caplog.records
    )
