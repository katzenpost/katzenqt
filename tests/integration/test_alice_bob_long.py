"""Long-message reassembly across multiple BACAP boxes.

Alice sends a text payload large enough to force
``SendOperation.serialize`` into its multi-box code path (one
``b'F'``/``b'C'`` chunk chain on an indirection substream plus a
``b'I'`` release into the parent stream). Bob must receive the
single reassembled ``GroupChatMessage``.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
Until the receive-side coalescer lands the read loop only commits
one ``ConversationLog`` per raw ``ReceivedPiece``, so the
``read`` action sees only the trailing ``b'F'`` chunk on its own
(an undecodable CBOR fragment) and times out.
"""
from __future__ import annotations

import os
import subprocess
import sys
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
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _expect_prefix(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(
        f"no stdout line starting with {prefix!r} in:\n---stdout---\n{stdout}"
    )


@pytest.mark.integration
def test_long_text_roundtrip(kpclientd_endpoint, tmp_path_factory):
    """A text payload that overflows one BACAP box must reach Bob
    intact as a single reassembled message."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # 3300 characters comfortably exceeds the 1530-byte chunk size
    # used by SendOperation.serialize, producing two b'C'/b'F'
    # chunks on an indirection substream plus the b'I' release on
    # the parent stream.
    long_text = (
        "Multi-box transmission test, please reassemble cleanly. "
    ) * 60
    assert len(long_text) > 3000

    t0 = time.monotonic()
    create = _run_role(
        alice_state, "create-conv", "demo", "alice", timeout=180.0,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    invite = _expect_prefix(create.stdout, "INVITE=")
    print(f"[long] alice created conv in {time.monotonic()-t0:.1f}s")

    accept = _run_role(
        bob_state, "accept-invite", "demo", "bob", "alice", invite, timeout=60.0,
    )
    assert accept.returncode == 0, accept.stdout + accept.stderr

    t0 = time.monotonic()
    send = _run_role(
        alice_state, "send", "demo", long_text, timeout=900.0,
    )
    assert send.returncode == 0, (
        f"alice send failed (rc={send.returncode}):\n"
        f"stdout tail:\n{send.stdout[-2000:]}\n"
        f"stderr tail:\n{send.stderr[-2000:]}"
    )
    assert "SENT" in send.stdout
    print(f"[long] alice multi-box send ACKed in {time.monotonic()-t0:.1f}s")

    t0 = time.monotonic()
    read = _run_role(
        bob_state, "read", "demo", "600", long_text, timeout=700.0,
    )
    assert read.returncode == 0, (
        f"bob read of long message failed:\n"
        f"stdout tail:\n{read.stdout[-2000:]}\n"
        f"stderr tail:\n{read.stderr[-2000:]}"
    )
    recv = _expect_prefix(read.stdout, "RECV=")
    assert recv == long_text
    print(f"[long] bob reassembled in {time.monotonic()-t0:.1f}s")
