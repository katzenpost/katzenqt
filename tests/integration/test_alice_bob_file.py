"""End-to-end file transfer over the mixnet.

Alice writes a known-content file to disk, hands it to the headless
``send-file`` verb, and Bob reassembles it through ``read-file``
into a per-state-file attachments directory. Both halves verify
SHA-256 equality so a corrupt reassembly would surface.

The payload is sized to just span two BACAP boxes, exercising the
multi-box substream, the parent's indirection release, and the courier
copy/reassembly path without paying for boxes that test nothing new.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
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


def _run_role(
    role_state: Path,
    *cli_args: str,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *_CONN_ARGS]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    """Find a logged line containing token; return the text after it. Results
    are emitted through logging (stderr) with a level/name prefix, so match by
    substring rather than line start."""
    for line in _output(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _bootstrap_voucher(alice_state: Path, bob_state: Path) -> None:
    """Establish contact via the Contact Voucher handshake: both create their
    own MessageStream, Bob mints a voucher, Alice inducts him, Bob joins.
    Afterwards Bob holds Alice's read cap and can read her stream."""
    for state, name in ((alice_state, "alice"), (bob_state, "bob")):
        create = _run_role(state, "create-conv", "demo", name, timeout=180.0)
        assert create.returncode == 0, create.stdout + create.stderr

    mint = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    assert mint.returncode == 0, mint.stdout + mint.stderr
    voucher = _expect_token(mint, "VOUCHER=")

    induct = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    assert induct.returncode == 0, induct.stdout + induct.stderr

    joined = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    assert joined.returncode == 0, joined.stdout + joined.stderr


@pytest.mark.integration
def test_file_roundtrip(kpclientd_endpoint, tmp_path_factory):
    """A ~2 KB file spans two BACAP boxes (one substream chain plus the
    parent's indirection release), the smallest payload that still
    exercises the multi-box copy/reassembly path. Bob must reconstruct
    the file byte-for-byte."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    src_dir = tmp_path_factory.mktemp("alice_outbox")
    dst_dir = tmp_path_factory.mktemp("bob_inbox")

    src = src_dir / "attachment.bin"
    # Deterministic but non-trivial content so byte equality is a real check.
    src.write_bytes(bytes((i * 17 + 11) & 0xFF for i in range(2_000)))
    assert src.stat().st_size == 2_000
    expected_sha = _sha256(src)

    _bootstrap_voucher(alice_state, bob_state)

    t0 = time.monotonic()
    send = _run_role(
        alice_state, "send-file", "demo", str(src),
        timeout=900.0,
    )
    assert send.returncode == 0 and "SENT" in _output(send), (
        f"send-file failed:\nstdout:\n{send.stdout}\nstderr:\n{send.stderr}"
    )
    print(f"[file] sent in {time.monotonic()-t0:.1f}s")

    t0 = time.monotonic()
    read = _run_role(
        bob_state, "read-file", "demo",
        "--to-dir", str(dst_dir),
        "--timeout", "600",
        timeout=700.0,
    )
    assert read.returncode == 0, (
        f"read-file failed:\nstdout tail:\n{read.stdout[-2000:]}\n"
        f"stderr tail:\n{read.stderr[-2000:]}"
    )
    recv_path = Path(_expect_token(read, "RECV_FILE="))
    assert recv_path.is_file(), f"reported path {recv_path} does not exist"
    assert _sha256(recv_path) == expected_sha
    print(f"[file] received in {time.monotonic()-t0:.1f}s -> {recv_path}")
