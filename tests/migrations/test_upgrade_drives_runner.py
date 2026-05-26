"""End-to-end migration smoke: an upgraded state file drives the headless runner.

Builds a fresh sqlite at a historical alembic revision, lets
``persistent.init_and_migrate`` bring it to head (which happens
automatically inside ``integration_runner.main``), then runs the
``info`` verb and a no-conversation ``read`` against it. Both verbs
short-circuit before touching kpclientd in this scenario (info
reads only from the local database; read against a missing
conversation returns 2 before opening a connection), so this test
runs without the docker mixnet and is always-on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
_HELPER = Path(__file__).with_name("_helper.py")


def test_info_then_read_against_upgraded_state(tmp_path):
    """Pre-stage a sqlite at the revision before AppSetting was
    introduced; the runner must upgrade it on first invocation and
    the resulting ``info`` line must parse as JSON with a non-empty
    alembic_revision and empty conversations list."""
    state_file = tmp_path / "state.sqlite3"

    # Stage the database at revision 93eef61c3c54 (one before head).
    env = {**os.environ, "KQT_STATE": str(state_file)}
    res = subprocess.run(
        [_PYTHON, str(_HELPER), "93eef61c3c54"],
        env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # Run the info verb; init_and_migrate fires first and brings the
    # database the rest of the way to head.
    info = subprocess.run(
        [_PYTHON, "-m", "katzenqt.integration_runner", "info"],
        env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    assert info.returncode == 0, info.stdout + info.stderr

    # The last JSON line on stdout is the info payload.
    json_line = None
    for line in info.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                json_line = json.loads(line)
            except json.JSONDecodeError:
                continue
    assert json_line is not None, (
        f"no JSON output found in info stdout:\n{info.stdout}"
    )
    assert json_line.get("alembic_revision")
    assert json_line.get("conversations") == []
    assert "wal" in json_line
    assert json_line["wal"].get("plaintext", 0) == 0
    assert json_line["wal"].get("mix", 0) == 0
    assert json_line["wal"].get("received_piece", 0) == 0

    # And a no-conversation read returns TIMEOUT cleanly (not an
    # exception or crash). Needs a live kpclientd, hence the
    # integration marker.
    read = subprocess.run(
        [_PYTHON, "-m", "katzenqt.integration_runner",
         "read", "no-such-conv", "1"],
        env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=60,
    )
    # An empty conversations list means the action prints ERROR= and
    # returns 2 (the runner's existing semantics).
    assert read.returncode == 2
    assert "conversation 'no-such-conv' not found" in read.stdout
