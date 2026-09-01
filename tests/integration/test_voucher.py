"""End-to-end Contact Voucher handshake over the docker mixnet.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).

Bob (the joiner) mints a Voucher over his MessageStream and publishes it
to VoucherStream box 0; Alice (the inductor) reads it, seals a reply
carrying her read cap to box 1, and adds Bob from his salt-mutated read
cap; Bob opens the reply, moves his write cap onto the salt-mutated
sequence, and adds Alice. Both roles run as separate subprocesses, each
with its own ``KQT_STATE`` SQLite, sharing only the mixnet.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable

# Connecting verbs require an explicit kpclientd connection. The docker mixnet's
# kpclientd listens on TCP 127.0.0.1:64331 (override via KATZENQT_KPCLIENTD_HOST
# / KATZENQT_KPCLIENTD_PORT, matching conftest).
_KP_ADDR = "{}:{}".format(
    os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1"),
    os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
)
_CONN_ARGS = ("--address", _KP_ADDR, "--network", "tcp")


def _run_role(role_state: Path, *cli_args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *_CONN_ARGS]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _assert_ok(proc: subprocess.CompletedProcess, what: str) -> None:
    assert proc.returncode == 0, (
        f"{what} failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    """Find a logged line containing token; return the text after it. The
    runner emits results through logging (stderr) with a level/name prefix,
    so match by substring rather than line start."""
    for line in _output(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@pytest.mark.integration
def test_voucher_handshake_then_bidirectional(kpclientd_endpoint, tmp_path_factory):
    """Full handshake, then a message each way. The Bob -> Alice leg is the
    crux: it rides Bob's salt-mutated write cap and Alice's salt-mutated
    read cap, which must address the same boxes."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # Each party provisions its own MessageStream (write/read cap).
    _assert_ok(_run_role(alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_run_role(bob_state, "create-conv", "demo", "bob"), "bob create-conv")

    # Bob mints a Voucher and publishes his payload to box 0.
    mint = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    _assert_ok(mint, "bob voucher-mint")
    voucher = _expect_token(mint, "VOUCHER=")
    assert voucher, "empty voucher"

    # Alice inducts Bob with the out-of-band voucher.
    induct = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    _assert_ok(induct, "alice voucher-induct")
    assert "INDUCTED=" in _output(induct)

    # Bob polls box 1, opens the reply, and joins.
    joined = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined, "bob voucher-await")
    assert "JOINED" in _output(joined)

    # Alice -> Bob: Bob holds Alice's read cap from the WhoReply.
    _assert_ok(_run_role(alice_state, "send", "demo", "hello from alice", timeout=300.0), "alice send")
    read_bob = _run_role(bob_state, "read", "demo", "180", "hello from alice", timeout=240.0)
    _assert_ok(read_bob, "bob read")
    assert _expect_token(read_bob, "RECV=") == "hello from alice"

    # Bob -> Alice on the salt-mutated stream: Alice holds Bob's mutated
    # read cap from induction. This is the cross-mutation crux.
    _assert_ok(_run_role(bob_state, "send", "demo", "hello from bob", timeout=300.0), "bob send")
    read_alice = _run_role(alice_state, "read", "demo", "180", "hello from bob", timeout=240.0)
    _assert_ok(read_alice, "alice read")
    assert _expect_token(read_alice, "RECV=") == "hello from bob"


@pytest.mark.integration
def test_voucher_await_resumes_after_crash(kpclientd_endpoint, tmp_path_factory):
    """Bob mints, then his first voucher-await is killed mid-poll (the
    PendingVoucher row survives on disk). After Alice inducts, a second
    voucher-await resumes from that row and joins, proving crash recovery."""
    alice_state = tmp_path_factory.mktemp("alice2") / "state"
    bob_state = tmp_path_factory.mktemp("bob2") / "state"

    _assert_ok(_run_role(alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_run_role(bob_state, "create-conv", "demo", "bob"), "bob create-conv")

    mint = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    _assert_ok(mint, "bob voucher-mint")
    voucher = _expect_token(mint, "VOUCHER=")

    # Kill the first await before Alice has replied: box 1 does not exist
    # yet, so the poll blocks and the subprocess is terminated on timeout.
    with pytest.raises(subprocess.TimeoutExpired):
        _run_role(bob_state, "voucher-await", "demo", timeout=25.0)

    # Now Alice replies.
    induct = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    _assert_ok(induct, "alice voucher-induct")

    # A fresh await must resume from the persisted PendingVoucher and join.
    joined = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined, "bob voucher-await (resumed)")
    assert "JOINED" in _output(joined)
