"""Re-test the original wedge scenario end-to-end: a message written while
the kpclientd link is down must ride out the outage and land once the daemon
comes back.

Deterministic shape, no mid-flight race timing required:

- Alice runs a long-lived chat-session that SLEEPs through the bounce and
  then READs ``m1`` (the sleep decouples her read window from the reconnect
  delay; the READ then has its full budget to observe the message that rode
  out the outage). The READ uses an explicit 1500 s budget: after a cold
  container restart the daemon takes up to ~9 min to re-attach to the mixnet
  gateway, far beyond the default 360 s chat-session read deadline (measured
  in kpclientd logs as the gap between container start and "Connected to
  gateway").
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
import time
from pathlib import Path

import pytest

from tests.integration._bounce_helpers import (
    run_role as _run_role,
    spawn_role as _spawn_role,
    bootstrap_voucher as _bootstrap_voucher,
)


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
        alice_state, "chat-session", "demo", "SLEEP:300", "READ:m1:1500",
        stdout_path=alice_out, stderr_path=alice_err,
    )
    # Bob proves the pair is connected and working (m0), then idles long
    # enough for us to take the daemon down and hold it.
    bob_proc = _spawn_role(
        bob_state, "chat-session", "demo",
        "SEND:m0", "SLEEP:120", "SEND:m1",
        stdout_path=bob_out, stderr_path=bob_err,
    )

    container_stopped = False
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
        container_stopped = True
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
        container_stopped = False
        _wait_reachable(120.0)

        alice_proc.wait(timeout=2100.0)
        bob_proc.wait(timeout=1500.0)
    except Exception:
        # This must run on ANY failure here, not just a timeout: an
        # AssertionError raised anywhere above this point (e.g. bob never
        # reaching m0, or the container refusing to go down) used to skip
        # straight past this cleanup, leaking both subprocesses and, if it
        # fired after the stop, leaving the SHARED kpclientd container dead
        # for every later test in the session.
        alice_proc.kill()
        bob_proc.kill()
        if container_stopped:
            try:
                _podman(["start", container])
            except Exception:
                pass  # best-effort; the original exception is what matters
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