"""Re-test the original wedge scenario end-to-end: a message written while
the kpclientd link is down must ride out the outage and land once the daemon
comes back.

Deterministic shape, no mid-flight race timing required:

- Alice runs a long-lived chat-session that SLEEPs through the bounce and
  then READs ``m1`` (the sleep decouples her read window from the reconnect
  delay; the READ then has its full budget to observe the message that rode
  out the outage).
- Bob runs a chat-session with steps ``SEND:m0`` (baseline, proves the pair
  is connected and working), ``SLEEP:120``, ``SEND:m1``.
- Once Bob's ``m0`` STEP_OK is observed, the test stops the kpclientd
  container, waits for the link to actually drop, sleeps past Bob's SLEEP (so
  ``m1`` is committed to Bob's local MixWAL while the link is DOWN), and only
  then starts the daemon again.
- Bob's SEND step blocks on its SentLog entry, so the drain must wait on the
  ``__mixnet_connected`` gate and complete via the thinclient reconnect +
  in-flight replay — exactly the ARQ ride-out the thinclient fixes provide.

This bounces the SHARED kpclientd daemon and must therefore run serial, NOT
concurrently with the other integration files. Skipped unless
``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = os.environ.get(
    "KATZENQT_INTEGRATION_PYTHON",
    str(_VENV_PY) if _VENV_PY.exists() else sys.executable,
)

_KP_ADDR = "{}:{}".format(
    os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1"),
    os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
)
_CONN_ARGS = ("--address", _KP_ADDR, "--network", "tcp")


def _run_role(role_state: Path, *cli_args: str, timeout: float = 300.0):
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *_CONN_ARGS]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    for line in (proc.stdout + proc.stderr).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _spawn_role(role_state: Path, *cli_args: str, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *_CONN_ARGS]
    return subprocess.Popen(
        cmd, env=env, cwd=str(_REPO_ROOT),
        stdout=open(stdout_path, "w"),
        stderr=open(stderr_path, "w"),
        text=True,
    )


def _bootstrap_voucher(alice_state: Path, bob_state: Path) -> None:
    """Establish mutual contact via the Contact Voucher handshake (Bob mints,
    Alice inducts, Bob joins), so both can read each other."""
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


def _kpclientd_reachable(timeout: float = 1.0) -> bool:
    host = os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1")
    port = int(os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_kpclientd_container() -> str:
    override = os.environ.get("KATZENQT_KPCLIENTD_CONTAINER")
    if override:
        return override
    proc = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman ps failed: {proc.stderr}")
    for name in proc.stdout.split():
        if name.endswith("-kpclientd-1"):
            return name
    raise RuntimeError(
        "no kpclientd container found via `podman ps --format {{.Names}}`; "
        "set KATZENQT_KPCLIENTD_CONTAINER to the kpclientd container name"
    )


def _podman(args) -> None:
    proc = subprocess.run(
        ["podman", *args], capture_output=True, text=True, check=False,
        timeout=120.0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args)} failed ({proc.returncode}): {proc.stderr}")


def _wait_reachable(deadline_s: float) -> None:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if _kpclientd_reachable():
            return
        time.sleep(1.0)
    raise AssertionError(f"kpclientd did not become reachable within {deadline_s:.0f}s")


@pytest.mark.integration
def test_write_survives_kpclientd_restart(kpclientd_endpoint, tmp_path_factory):
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("bounce_logs")
    _bootstrap_voucher(alice_state, bob_state)
    container = _find_kpclientd_container()

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"
    bob_out = log_dir / "bob.out"
    bob_err = log_dir / "bob.err"

    alice_proc = _spawn_role(
        alice_state, "chat-session", "demo", "SLEEP:300", "READ:m1",
        stdout_path=alice_out, stderr_path=alice_err,
    )
    # Bob proves the pair is connected and working (m0), then idles long
    # enough for us to take the daemon down and hold it.
    bob_proc = _spawn_role(
        bob_state, "chat-session", "demo",
        "SEND:m0", "SLEEP:120", "SEND:m1",
        stdout_path=bob_out, stderr_path=bob_err,
    )

    try:
        # Wait for Bob's baseline send to be ACKed (SentLog); at this point
        # both chat-sessions are connected to a live daemon.
        t_m0 = time.time()
        deadline0 = t_m0 + 240.0
        while time.time() < deadline0:
            if "STEP_OK:0:SEND:m0" in bob_err.read_text():
                break
            time.sleep(1.0)
        else:
            raise AssertionError(
                "bob never completed the baseline SEND:m0\n"
                f"bob stderr:\n{bob_err.read_text()[-3000:]}"
            )

        # Kill the daemon and confirm the link is actually gone.
        _podman(["stop", container])
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if not _kpclientd_reachable():
                break
            time.sleep(0.5)
        else:
            raise AssertionError("kpclientd still reachable after podman stop")

        # Hold the daemon down until AFTER Bob's SLEEP:120 elapsed, so the
        # SEND:m1 commit lands in his local MixWAL while the link is down.
        # Margins: the stop/unreachable dance took ~5-20s of the 120s budget.
        time.sleep(130.0)

        _podman(["start", container])
        _wait_reachable(120.0)

        alice_proc.wait(timeout=1500.0)
        bob_proc.wait(timeout=1500.0)
    except subprocess.TimeoutExpired:
        alice_proc.kill()
        bob_proc.kill()
        raise

    alice_all = alice_out.read_text() + alice_err.read_text()
    bob_all = bob_out.read_text() + bob_err.read_text()
    for who, text in (("alice", alice_all), ("bob", bob_all)):
        for line in text.splitlines():
            if any(t in line for t in ("STEP_OK", "STEP_FAIL", "STEP_POLL", "SESSION_DONE")):
                print(f"[bounce][{who}] {line}")

    assert alice_proc.returncode == 0, (
        f"alice chat-session failed rc={alice_proc.returncode}\n{alice_err.read_text()[-3000:]}"
    )
    assert bob_proc.returncode == 0, (
        f"bob chat-session failed rc={bob_proc.returncode}\n{bob_err.read_text()[-3000:]}"
    )
    # The write that rode out the kpclientd restart must have landed.
    assert "STEP_OK:2:SEND:m1" in bob_all, (
        f"bob's SEND:m1 did not complete (ARQ did not ride out the restart)\n"
        f"bob stderr:\n{bob_err.read_text()[-6000:]}"
    )
    assert "STEP_OK:1:READ:m1" in alice_all, (
        f"alice never read m1 after the restart\n"
        f"alice stderr:\n{alice_err.read_text()[-6000:]}"
    )
    assert "SESSION_DONE" in alice_all and "SESSION_DONE" in bob_all, (
        f"clean-shutdown sentinel missing\n"
        f"alice stderr:\n{alice_err.read_text()[-3000:]}\n"
        f"bob stderr:\n{bob_err.read_text()[-3000:]}"
    )