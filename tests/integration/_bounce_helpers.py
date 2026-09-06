"""Shared subprocess/voucher helpers for the restart and kpclientd-bounce
integration tests.

Previously duplicated verbatim between test_restart.py and
test_kpclientd_restart.py, where the two copies had already started to
drift (test_restart.py's _expect_token routed through a _combined()
helper; test_kpclientd_restart.py's inlined `proc.stdout + proc.stderr`
directly). One copy here so a fix to how these subprocesses are launched
or their output parsed can't be applied to one file and missed in the
other.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = REPO_ROOT / ".venv" / "bin" / "python3"
PYTHON = os.environ.get(
    "KATZENQT_INTEGRATION_PYTHON",
    str(_VENV_PY) if _VENV_PY.exists() else sys.executable,
)

# Connecting verbs require an explicit kpclientd connection. The docker mixnet's
# kpclientd listens on TCP 127.0.0.1:64331 (override via KATZENQT_KPCLIENTD_HOST
# / KATZENQT_KPCLIENTD_PORT, matching conftest).
KP_ADDR = "{}:{}".format(
    os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1"),
    os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
)
CONN_ARGS = ("--address", KP_ADDR, "--network", "tcp")


def run_role(role_state: Path, *cli_args: str, timeout: float = 300.0):
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *CONN_ARGS]
    return subprocess.run(
        cmd, env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def spawn_role(role_state: Path, *cli_args: str, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    """Popen variant for long-running chat-session subprocesses that we
    want running in parallel. We redirect stdout/stderr to files instead
    of pipes to avoid the classic 64 KB pipe-buffer deadlock: when one
    subprocess fills its stdout pipe, it blocks on write, and if the
    parent is `communicate`-ing a different subprocess, the blocked one
    can starve long enough for its background read loop to stall.
    """
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *CONN_ARGS]
    return subprocess.Popen(
        cmd, env=env, cwd=str(REPO_ROOT),
        stdout=open(stdout_path, "w"),
        stderr=open(stderr_path, "w"),
        text=True,
    )


def combined(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    """Find a logged line containing token; return the text after it. Results
    go through logging (stderr) with a level/name prefix, so match by
    substring."""
    for line in combined(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def bootstrap_voucher(alice_state: Path, bob_state: Path) -> None:
    """Establish mutual contact via the Contact Voucher handshake. Bob mints a
    voucher over his stream, Alice inducts him (gaining his salt-mutated read
    cap) and replies with her read cap, and Bob joins (gaining hers). Both can
    then read each other, the bidirectional state the restart tests exercise."""
    for state, name in ((alice_state, "alice"), (bob_state, "bob")):
        create = run_role(state, "create-conv", "demo", name, timeout=180.0)
        assert create.returncode == 0, create.stdout + create.stderr
    mint = run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    assert mint.returncode == 0, mint.stdout + mint.stderr
    voucher = expect_token(mint, "VOUCHER=")
    induct = run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    assert induct.returncode == 0, induct.stdout + induct.stderr
    joined = run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    assert joined.returncode == 0, joined.stdout + joined.stderr
