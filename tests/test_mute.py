"""Per-conversation mute is a local notification preference stored in the
AppSetting key/value table under "mute:<conversation_id>". These tests pin
the round-trip, the default (unmuted), independence across conversations,
and the exact settings key format other clients mirror for parity.

is_muted/set_muted go through the async engine (see persistent.py) since
is_muted is read from the message-receive hot path; the verification helpers
below still read back through the sync engine, which is the same on-disk
database and sees the same committed rows either way.
"""
import pytest
from sqlmodel import Session, select

from katzenqt import persistent


@pytest.mark.asyncio
async def test_default_is_unmuted():
    assert await persistent.is_muted(1) is False


@pytest.mark.asyncio
async def test_set_muted_round_trip():
    await persistent.set_muted(7, True)
    assert await persistent.is_muted(7) is True
    await persistent.set_muted(7, False)
    assert await persistent.is_muted(7) is False


@pytest.mark.asyncio
async def test_unmute_deletes_the_row():
    await persistent.set_muted(7, True)
    with Session(persistent._engine_sync) as sess:
        assert sess.get(persistent.AppSetting, "mute:7") is not None
    await persistent.set_muted(7, False)
    with Session(persistent._engine_sync) as sess:
        assert sess.get(persistent.AppSetting, "mute:7") is None


@pytest.mark.asyncio
async def test_conversations_are_independent():
    await persistent.set_muted(1, True)
    assert await persistent.is_muted(1) is True
    assert await persistent.is_muted(2) is False


@pytest.mark.asyncio
async def test_key_format_and_value():
    await persistent.set_muted(42, True)
    with Session(persistent._engine_sync) as sess:
        row = sess.get(persistent.AppSetting, "mute:42")
    assert row is not None
    assert row.type == "str"
    assert row.value == "1"


@pytest.mark.asyncio
async def test_muting_is_idempotent():
    await persistent.set_muted(5, True)
    await persistent.set_muted(5, True)
    assert await persistent.is_muted(5) is True
    rows = _mute_rows()
    assert rows.count("mute:5") == 1


@pytest.mark.asyncio
async def test_unmuting_absent_row_is_noop():
    await persistent.set_muted(9, False)
    assert await persistent.is_muted(9) is False


def _mute_rows():
    with Session(persistent._engine_sync) as sess:
        return [
            r.id
            for r in sess.exec(select(persistent.AppSetting))
            if r.id.startswith(persistent.MUTE_SETTING_PREFIX)
        ]
