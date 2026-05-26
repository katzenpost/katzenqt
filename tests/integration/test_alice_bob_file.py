"""End-to-end file transfer over the mixnet.

Alice writes a known-content file to disk, hands it to the headless
``send-file`` verb, and Bob reassembles it through ``read-file``
into a per-state-file attachments directory. Both halves verify
SHA-256 equality so a corrupt reassembly would surface.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
Both subcommands are introduced in the implementation commit that
follows; with the env var set this file's tests fail until then.
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


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _bootstrap_invitation(alice_state: Path, bob_state: Path) -> None:
    create = _run_role(
        alice_state, "create-conv", "demo", "alice", timeout=180.0,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    invite = _expect_prefix(create.stdout, "INVITE=")
    accept = _run_role(
        bob_state, "accept-invite", "demo", "bob", "alice", invite, timeout=60.0,
    )
    assert accept.returncode == 0, accept.stdout + accept.stderr


@pytest.mark.integration
def test_small_file_roundtrip(kpclientd_endpoint, tmp_path_factory):
    """A 4 KB file spans roughly three BACAP boxes (one substream
    chain and the parent's indirection release). Bob must reconstruct
    the file byte-for-byte."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    src_dir = tmp_path_factory.mktemp("alice_outbox")
    dst_dir = tmp_path_factory.mktemp("bob_inbox")

    src = src_dir / "small.txt"
    src.write_bytes(b"Mixnet attachment test, small payload. " * 110)
    assert 3500 < src.stat().st_size < 5000
    expected_sha = _sha256(src)

    _bootstrap_invitation(alice_state, bob_state)

    t0 = time.monotonic()
    send = _run_role(
        alice_state, "send-file", "demo", str(src),
        timeout=900.0,
    )
    assert send.returncode == 0 and "SENT" in send.stdout, (
        f"send-file failed:\nstdout:\n{send.stdout}\nstderr:\n{send.stderr}"
    )
    print(f"[file-small] sent in {time.monotonic()-t0:.1f}s")

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
    recv_path = Path(_expect_prefix(read.stdout, "RECV_FILE="))
    assert recv_path.is_file(), f"reported path {recv_path} does not exist"
    assert _sha256(recv_path) == expected_sha
    print(f"[file-small] received in {time.monotonic()-t0:.1f}s -> {recv_path}")


@pytest.mark.integration
def test_large_file_roundtrip(kpclientd_endpoint, tmp_path_factory):
    """A 12 KB file spans more than four BACAP boxes; combined with
    the substream's all-or-nothing release this exercises the deeper
    end of the reassembly chain."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    src_dir = tmp_path_factory.mktemp("alice_outbox")
    dst_dir = tmp_path_factory.mktemp("bob_inbox")

    src = src_dir / "big.bin"
    # Deterministic but non-trivial content so byte equality is a real check.
    src.write_bytes(bytes((i * 17 + 11) & 0xFF for i in range(12_000)))
    assert src.stat().st_size == 12_000
    expected_sha = _sha256(src)

    _bootstrap_invitation(alice_state, bob_state)

    t0 = time.monotonic()
    send = _run_role(
        alice_state, "send-file", "demo", str(src),
        timeout=1800.0,
    )
    assert send.returncode == 0 and "SENT" in send.stdout, (
        f"send-file (large) failed:\nstdout:\n{send.stdout}\n"
        f"stderr:\n{send.stderr}"
    )
    print(f"[file-large] sent in {time.monotonic()-t0:.1f}s")

    t0 = time.monotonic()
    read = _run_role(
        bob_state, "read-file", "demo",
        "--to-dir", str(dst_dir),
        "--timeout", "1200",
        timeout=1300.0,
    )
    assert read.returncode == 0, (
        f"read-file (large) failed:\nstdout tail:\n{read.stdout[-2000:]}\n"
        f"stderr tail:\n{read.stderr[-2000:]}"
    )
    recv_path = Path(_expect_prefix(read.stdout, "RECV_FILE="))
    assert recv_path.is_file()
    assert _sha256(recv_path) == expected_sha
    print(f"[file-large] received in {time.monotonic()-t0:.1f}s -> {recv_path}")
