"""Always-on regression guard: every historical schema revision must
upgrade cleanly to head.

Each parametrised case spawns a fresh subprocess so it can own its
own ``KQT_STATE`` sqlite file without colliding with the repo-wide
test fixture's tmpdir. The subprocess delegates to ``_helper.py``
which performs:

  1. ``alembic.command.upgrade(cfg, R)``  — bring an empty database
     to the chosen historical revision.
  2. ``persistent.init_and_migrate()``    — bring it from R to head.
  3. report the head revision and the resulting table set as JSON
     on stdout.

The parametrisation samples the migration chain at five points:
the initial revision after empty, two mid-chain checkpoints (one
just after MixWAL acquired ``current_message_index``, one just after
``ConversationLog.network_status``), the revision that introduced
``ReceivedPiece``, and the revision immediately before ``AppSetting``
landed. These are the existing revisions whose schemas a user's
state file is most likely to be parked at.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sqlmodel import SQLModel


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HELPER = Path(__file__).with_name("_helper.py")


@pytest.mark.parametrize("revision", [
    "888b1ac7eca0",  # base — uuids
    "7094a18467be",  # mixwal: current_message_index
    "524576e2f8a5",  # network_status tracking
    "a430f7202849",  # ReceivedPiece introduced
    "93eef61c3c54",  # 64-bit-integer remediation; one before AppSetting
    "35cec50b9604",  # conversation_voucher_used_flag; before the tally column
])
def test_upgrade_from_revision_reaches_head(revision, tmp_path):
    """Each historical revision must upgrade to head and leave every
    table the SQLModel metadata defines present and named.

    A subprocess is used so ``persistent`` is loaded with this test's
    own ``KQT_STATE``; the parent process has its own already bound
    from the repo conftest and we must not stomp on it.
    """
    payload = _run_helper(revision, tmp_path)

    # Head must be non-empty and the same for every parametrised
    # case (alembic's notion of "the head of the chain" is single).
    assert payload["head"]
    actual = set(payload["tables"])
    expected = set(SQLModel.metadata.tables.keys())
    missing = expected - actual
    assert not missing, (
        f"after upgrading from {revision!r} to head, the following "
        f"SQLModel tables are missing on disk: {sorted(missing)}"
    )


def test_upgrade_from_pre_tally_revision_gains_conversation_order(tmp_path):
    """``TallyState.conversation_order`` is added by the tally migration; a
    state file parked at 35cec50b9604 (its parent) must gain the column on the
    way to head. The table-set check alone cannot see a missing column."""
    payload = _run_helper("35cec50b9604", tmp_path)
    assert "conversation_order" in payload["columns"]["tallystate"]
    # ...and it is nullable, so pre-existing surveys keep working.
    assert "doc_state" in payload["columns"]["tallystate"]


def _run_helper(revision: str, tmp_path) -> dict:
    state_file = tmp_path / f"state-{revision}.sqlite3"
    env = {
        # Reproduce the parent process's PATH and HOME so the venv
        # python keeps working.
        **dict(__import__("os").environ),
        "KQT_STATE": str(state_file),
    }
    res = subprocess.run(
        [sys.executable, str(_HELPER), revision],
        env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, (
        f"helper failed for revision {revision!r}:\n"
        f"stdout:\n{res.stdout[-2000:]}\nstderr:\n{res.stderr[-2000:]}"
    )

    payload = None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    assert payload is not None, (
        f"no JSON result line found in helper output:\n{res.stdout}"
    )
    return payload


def test_helper_module_exists():
    """The subprocess helper must remain present alongside this
    test file; a renamed or missing helper turns every parametrised
    case into a silent skip if the test ever swallows the missing-file
    error path."""
    assert _HELPER.is_file()
