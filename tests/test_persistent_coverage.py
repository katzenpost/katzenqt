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
    try:
        resolved = persistent._resolve_alembic_ini()
    except ModuleNotFoundError:
        pytest.skip("this tree reads alembic.ini as package data only")
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


