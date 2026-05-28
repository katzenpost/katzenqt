"""End-to-end: Alice sends to her channel, Bob reads from that channel.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).

The two roles run as separate subprocesses — each with its own
``KQT_STATE`` SQLite — so they share nothing but the mixnet. Both connect
to the same kpclientd daemon at 127.0.0.1:64331, each under its own thin
client AppID (the daemon's listener demultiplexes).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable


def _run_role(
    role_state: Path,
    *cli_args: str,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess:
    """Run one integration_runner subcommand under role_state's KQT_STATE."""
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args]
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _expect_prefix(stdout: str, prefix: str) -> str:
    """Find the first stdout line beginning with prefix; return the tail."""
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(
        f"no stdout line starting with {prefix!r} in:\n---stdout---\n{stdout}"
    )


@pytest.mark.integration
def test_alice_sends_bob_reads(kpclientd_endpoint, tmp_path_factory):
    """Alice creates a channel, shares an invite, sends a message; Bob
    accepts the invite, polls, and must receive Alice's message."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # 1. Alice creates her conversation and emits an invite. This blocks
    # until provision_read_caps has filled in her WriteCap / ReadCap via
    # kpclientd.new_keypair, so allow a generous budget for PKI + daemon
    # startup.
    t0 = time.monotonic()
    create = _run_role(
        alice_state,
        "create-conv", "demo", "alice",
        timeout=180.0,
    )
    assert create.returncode == 0, (
        f"alice create-conv failed (rc={create.returncode}):\n"
        f"stdout:\n{create.stdout}\nstderr:\n{create.stderr}"
    )
    invite = _expect_prefix(create.stdout, "INVITE=")
    print(f"[integration] alice created conv in {time.monotonic()-t0:.1f}s "
          f"(invite={invite[:32]}...)")

    # 2. Bob accepts Alice's invite. This is a pure DB insert, no network
    # round trip required.
    accept = _run_role(
        bob_state,
        "accept-invite", "demo", "bob", "alice", invite,
        timeout=30.0,
    )
    assert accept.returncode == 0, (
        f"bob accept-invite failed (rc={accept.returncode}):\n"
        f"stdout:\n{accept.stdout}\nstderr:\n{accept.stderr}"
    )
    assert "ACCEPTED" in accept.stdout

    # 3. Alice sends a message. This blocks until the courier ACKs,
    # i.e., SentLog contains the relevant PWAL id.
    t0 = time.monotonic()
    send = _run_role(
        alice_state,
        "send", "demo", "hello from alice",
        timeout=300.0,
    )
    assert send.returncode == 0, (
        f"alice send failed (rc={send.returncode}):\n"
        f"stdout:\n{send.stdout}\nstderr:\n{send.stderr}"
    )
    assert "SENT" in send.stdout
    print(f"[integration] alice send ACKed in {time.monotonic()-t0:.1f}s")

    # 4. Bob polls for the incoming message. Give the mixnet another
    # full round of send+read latency — in the docker setup with 2-minute
    # epochs this is usually <30s but can spike.
    t0 = time.monotonic()
    read = _run_role(
        bob_state,
        "read", "demo", "120",
        timeout=180.0,
    )
    assert read.returncode == 0, (
        f"bob read failed (rc={read.returncode}):\n"
        f"stdout:\n{read.stdout}\nstderr:\n{read.stderr}"
    )
    recv = _expect_prefix(read.stdout, "RECV=")
    print(f"[integration] bob received in {time.monotonic()-t0:.1f}s: {recv!r}")

    assert recv == "hello from alice", (
        f"bob received {recv!r}, expected 'hello from alice'"
    )
