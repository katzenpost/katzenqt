from __future__ import annotations

import importlib.resources
import uuid
from pathlib import Path

import pytest

from katzenqt import persistent


def test_alembic_ini_falls_back_to_the_source_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_name: str) -> object:
        raise ModuleNotFoundError("katzenqt")

    monkeypatch.setattr(importlib.resources, "files", boom)
    resolved = persistent._resolve_alembic_ini()
    assert isinstance(resolved, Path)
    assert resolved.name == "alembic.ini"
    assert resolved.parent.name == "config"


def test_alembic_ini_prefers_the_packaged_copy() -> None:
    resolved = persistent._resolve_alembic_ini()
    assert resolved.name == "alembic.ini"
    assert resolved.is_file()


def test_write_cap_select_by_uuid_filters_on_the_primary_key() -> None:
    wanted = uuid.uuid4()
    stmt = persistent.WriteCapWAL.get_by_bacap_uuid(wanted)
    rendered = str(stmt)
    assert "writecapwal" in rendered.lower()
    assert "WHERE" in rendered
    assert wanted in stmt.compile().params.values()


@pytest.mark.asyncio
async def test_resend_queue_from_disk_is_empty_without_rows() -> None:
    assert await persistent.MixWAL.resend_queue_from_disk() == set()


@pytest.mark.asyncio
async def test_resend_queue_from_disk_reports_stored_streams() -> None:
    stream = uuid.uuid4()
    row = persistent.MixWAL(
        id=uuid.uuid4(),
        plaintextwal=uuid.uuid4(),
        bacap_stream=stream,
        envelope_hash=b"\x00" * 32,
        encrypted_payload=b"payload",
        envelope_descriptor=b"descriptor",
        current_message_index=b"\x00" * 104,
        next_message_index=b"\x01" * 104,
        is_read=False,
    )
    async with persistent.asession() as sess:
        sess.add(row)
        await sess.commit()
    assert await persistent.MixWAL.resend_queue_from_disk() == {stream}
