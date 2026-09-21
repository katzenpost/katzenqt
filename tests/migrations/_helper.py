"""Subprocess helper for ``test_upgrade.py``.

Invoked as ``python tests/migrations/_helper.py <revision>`` with
``KQT_STATE`` set to a tmp sqlite path. Brings an empty database to
the target revision via ``alembic.command.upgrade``, then forward to
head via ``persistent.init_and_migrate``, then prints one line of
JSON describing the resulting head revision and table set.

A subprocess is necessary because ``persistent._sql_url`` is bound
at module import time from ``$KQT_STATE`` and a single python
process cannot re-target it without wholesale reimport gymnastics.
"""
from __future__ import annotations

import json
import sys

import alembic.command
import sqlalchemy
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlmodel import SQLModel

from katzenqt import persistent


def main(revision: str) -> int:
    cfg = persistent._alembic_cfg

    alembic.command.upgrade(cfg, revision)
    with persistent._engine_sync.connect() as conn:
        ctx = MigrationContext.configure(conn)
        cur = ctx.get_current_revision()
    if cur != revision:
        print(
            f"ERROR: after upgrade to {revision!r}, current is {cur!r}",
            file=sys.stderr,
        )
        return 3

    persistent.init_and_migrate()
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with persistent._engine_sync.connect() as conn:
        ctx = MigrationContext.configure(conn)
        cur = ctx.get_current_revision()
    if cur != head:
        print(
            f"ERROR: after init_and_migrate, current is {cur!r}, "
            f"expected head {head!r}",
            file=sys.stderr,
        )
        return 4

    inspector = sqlalchemy.inspect(persistent._engine_sync)
    actual = sorted(set(inspector.get_table_names()) - {"alembic_version"})
    expected = sorted(SQLModel.metadata.tables.keys())

    print(json.dumps({
        "revision_after_partial": revision,
        "head": head,
        "tables": actual,
        "expected": expected,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
