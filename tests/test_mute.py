"""Per-conversation mute is a local notification preference stored in the
AppSetting key/value table under "mute:<conversation_id>". These tests pin
the round-trip, the default (unmuted), independence across conversations,
and the exact settings key format other clients mirror for parity.
"""
from sqlmodel import Session, select

from katzenqt import persistent


def test_default_is_unmuted():
    assert persistent.is_muted(1) is False


def test_set_muted_round_trip():
    persistent.set_muted(7, True)
    assert persistent.is_muted(7) is True
    persistent.set_muted(7, False)
    assert persistent.is_muted(7) is False


def test_unmute_deletes_the_row():
    persistent.set_muted(7, True)
    with Session(persistent._engine_sync) as sess:
        assert sess.get(persistent.AppSetting, "mute:7") is not None
    persistent.set_muted(7, False)
    with Session(persistent._engine_sync) as sess:
        assert sess.get(persistent.AppSetting, "mute:7") is None


def test_conversations_are_independent():
    persistent.set_muted(1, True)
    assert persistent.is_muted(1) is True
    assert persistent.is_muted(2) is False


def test_key_format_and_value():
    persistent.set_muted(42, True)
    with Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, "mute:42")
    assert row is not None
    assert row.type == "str"
    assert row.value == "1"


def test_muting_is_idempotent():
    persistent.set_muted(5, True)
    persistent.set_muted(5, True)
    assert persistent.is_muted(5) is True
    rows = _mute_rows()
    assert rows.count("mute:5") == 1


def test_unmuting_absent_row_is_noop():
    persistent.set_muted(9, False)
    assert persistent.is_muted(9) is False


def _mute_rows():
    with Session(persistent._engine_sync) as sess:
        return [
            r.id
            for r in sess.exec(select(persistent.AppSetting))
            if r.id.startswith(persistent.MUTE_SETTING_PREFIX)
        ]
