"""End-to-end tally over the mixnet.

Alice creates a three-slot approval survey; Alice and Bob each cast a vote;
both then read the tally. The derived counts must be identical on the two
peers (convergence) and match the hand-computed expectation. This exercises
the whole protocol path over the real transport: the create broadcast, the
semantic vote events keyed to the authenticated sender, the receive-side
dispatch into the controller, the persisted CRDT state, and the pure tally.

Contact is established with the Contact Voucher handshake, which makes the two
peers readers of each other, so votes flow both ways.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable


def _run_role(role_state: Path, *cli_args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    for line in _output(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _bootstrap_voucher(alice_state: Path, bob_state: Path) -> None:
    """Establish mutual contact: both create a stream, Bob mints a voucher,
    Alice inducts him, Bob joins. Afterwards each holds the other's read cap."""
    for state, name in ((alice_state, "alice"), (bob_state, "bob")):
        create = _run_role(state, "create-conv", "demo", name, timeout=180.0)
        assert create.returncode == 0, _output(create)

    mint = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    assert mint.returncode == 0, _output(mint)
    voucher = _expect_token(mint, "VOUCHER=")

    induct = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    assert induct.returncode == 0, _output(induct)

    joined = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    assert joined.returncode == 0, _output(joined)


def _slots_by_id(tally_json: dict) -> dict:
    return {s["slot_id"]: s for s in tally_json["slots"]}


@pytest.mark.integration
def test_tally_converges_across_peers(kpclientd_endpoint, tmp_path_factory):
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    _bootstrap_voucher(alice_state, bob_state)

    # Alice creates a three-slot approval survey and broadcasts it.
    create = _run_role(
        alice_state, "tally-create", "demo", "lunch?",
        "--mode", "approval", "--slot", "A", "--slot", "B", "--slot", "C",
        timeout=600.0,
    )
    assert create.returncode == 0, _output(create)
    survey = _expect_token(create, "TALLY_CREATED=")

    # Bob votes for A and C (he must first receive the survey).
    bob_vote = _run_role(
        bob_state, "tally-vote", "demo", "--survey", survey,
        "--slot", "s0=yes", "--slot", "s2=yes", "--timeout", "600",
        timeout=900.0,
    )
    assert bob_vote.returncode == 0, _output(bob_vote)
    assert "VOTED" in _output(bob_vote)

    # Alice votes for A and B.
    alice_vote = _run_role(
        alice_state, "tally-vote", "demo", "--survey", survey,
        "--slot", "s0=yes", "--slot", "s1=yes", "--timeout", "600",
        timeout=900.0,
    )
    assert alice_vote.returncode == 0, _output(alice_vote)
    assert "VOTED" in _output(alice_vote)

    # Both read the tally, waiting for two voters.
    alice_res = _run_role(
        alice_state, "tally-result", "demo", "--survey", survey,
        "--expect-voters", "2", "--timeout", "600", timeout=700.0,
    )
    assert alice_res.returncode == 0, _output(alice_res)
    bob_res = _run_role(
        bob_state, "tally-result", "demo", "--survey", survey,
        "--expect-voters", "2", "--timeout", "600", timeout=700.0,
    )
    assert bob_res.returncode == 0, _output(bob_res)

    alice_tally = json.loads(_expect_token(alice_res, "TALLY="))
    bob_tally = json.loads(_expect_token(bob_res, "TALLY="))

    # Convergence: the two peers agree slot for slot.
    assert _slots_by_id(alice_tally) == _slots_by_id(bob_tally)
    assert alice_tally["n_voters"] == 2

    # And the counts match the hand-computed expectation.
    by = _slots_by_id(alice_tally)
    assert (by["s0"]["yes"], by["s0"]["no"]) == (2, 0)  # both for A
    assert (by["s1"]["yes"], by["s1"]["no"]) == (1, 1)  # Alice only for B
    assert (by["s2"]["yes"], by["s2"]["no"]) == (1, 1)  # Bob only for C
