"""Live confirmation of network.py's reconnect-triggered read watchdog.

This targets the one fix in the review-findings-fixes branch that could
not be confidently verified against fakes alone: _await_read_reply races
an in-flight read against on_connection_status's _reconnect_event swap,
so a read stranded by a real kpclientd bounce should recover within the
~30s reconnect grace period, not the old flat 120s scan (which falsely
fired on ordinary idle waits) or the new, much larger 1200s backstop
(which would mean the reconnect-detection path never engaged at all).

Bounces the SHARED kpclientd daemon; run serial, not concurrently with
the other integration files. Skipped unless KATZENQT_DOCKER_INTEGRATION=1
(see conftest.py).
"""
from __future__ import annotations

import time

import pytest

from tests.integration._bounce_helpers import (
    bootstrap_voucher, spawn_role, run_role,
    kpclientd_reachable, find_kpclientd_container, podman, wait_reachable,
)


@pytest.mark.integration
def test_read_recovers_promptly_after_kpclientd_reconnect(kpclientd_endpoint, tmp_path_factory):
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("watchdog_logs")
    bootstrap_voucher(alice_state, bob_state)
    container = find_kpclientd_container()

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"

    # Alice waits for a message Bob hasn't sent yet: a genuine in-flight
    # read sitting in the daemon's stop-and-wait ARQ when we bounce it.
    alice_proc = spawn_role(
        alice_state, "chat-session", "demo", "READ:m1:600",
        stdout_path=alice_out, stderr_path=alice_err,
    )

    container_stopped = False
    try:
        # Give Alice's read time to actually reach the daemon and be
        # registered as in-flight before we pull the rug.
        time.sleep(10.0)

        podman(["stop", container])
        container_stopped = True
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if not kpclientd_reachable():
                break
            time.sleep(0.5)
        else:
            raise AssertionError("kpclientd still reachable after podman stop")

        podman(["start", container])
        container_stopped = False
        wait_reachable(120.0)

        # Bob's send happens only AFTER the daemon is back: Alice's read
        # task is still pending at reconnect time, so the reconnect_event
        # is guaranteed to fire before the read itself resolves, forcing
        # the grace-period branch rather than racing past it.
        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=300.0)
        assert send.returncode == 0, send.stdout + send.stderr

        alice_proc.wait(timeout=300.0)
    except Exception:
        alice_proc.kill()
        if container_stopped:
            try:
                podman(["start", container])
            except Exception:
                pass
        raise

    alice_all = alice_out.read_text() + alice_err.read_text()
    for line in alice_all.splitlines():
        if any(t in line for t in ("STEP_OK", "STEP_FAIL", "SESSION_DONE", "reconnected mid-wait")):
            print(f"[watchdog] {line}")

    assert alice_proc.returncode == 0, (
        f"alice chat-session failed rc={alice_proc.returncode}\n{alice_err.read_text()[-4000:]}"
    )
    assert "STEP_OK:0:READ:m1" in alice_all, (
        f"alice never read m1 after the reconnect\n{alice_err.read_text()[-6000:]}"
    )
    # The direct confirmation: the reconnect-generation detection in
    # _await_read_reply actually fired for this read, not just that it
    # eventually succeeded via some other path.
    assert "daemon reconnected mid-wait for bacap_stream=" in alice_all, (
        "the reconnect-triggered watchdog path never engaged for this read; "
        "either it recovered some other way, or the fix regressed\n"
        f"{alice_err.read_text()[-6000:]}"
    )
