"""Shared fixtures for katzenqt tests.

The parent `/home/human/katzenqt/conftest.py` has already pointed
`KQT_STATE` at a tmpdir before `persistent` was imported, so all sessions
share a disposable SQLite file. Fixtures in this module prepare schema and
wipe rows between tests.
"""
import pytest
from sqlmodel import SQLModel

from katzenqt import persistent


@pytest.fixture(autouse=True)
def _fresh_tables():
    """Drop + recreate all tables before every test.

    We skip alembic (it would try to read the repo's migrations/) and use
    sqlmodel's metadata directly, which is the source of truth the test
    subjects (MixWAL, PlaintextWAL, etc.) are actually defined against.
    """
    SQLModel.metadata.drop_all(persistent._engine_sync)
    SQLModel.metadata.create_all(persistent._engine_sync)
    yield
