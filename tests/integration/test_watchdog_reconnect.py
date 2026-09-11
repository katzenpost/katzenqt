"""Verify delivery after restarting the shared daemon; run serially."""
from __future__ import annotations

import time

import pytest

from tests.integration._bounce_helpers import (
    bootstrap_voucher, spawn_role, run_role,
    kpclientd_reachable, find_kpclientd_container, podman, wait_reachable,
)


@pytest.mark.integration
@pytest.mark.serial_docker
def test_read_recovers_after_full_kpclientd_restart(kpclientd_endpoint, tmp_path_factory):
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("watchdog_logs")
    bootstrap_voucher(alice_state, bob_state)
    container = find_kpclientd_container()

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"

    # Alice waits for a message Bob hasn't sent yet: a genuine in-flight
    # read sitting in the daemon's stop-and-wait ARQ when we bounce it.
    # 1500s: after a cold container restart the daemon takes up to ~9 min
    # to re-attach to the mixnet gateway, well beyond the default 360s
    # chat-session deadline.
    alice_proc = spawn_role(
        alice_state, "chat-session", "demo", "READ:m1:1500",
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

        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=900.0)
        assert send.returncode == 0, send.stdout + send.stderr

        alice_proc.wait(timeout=2100.0)
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
        f"alice never read m1 after the restart\n{alice_err.read_text()[-6000:]}"
    )
    assert "SESSION_DONE" in alice_all, (
        f"clean-shutdown sentinel missing\n{alice_err.read_text()[-3000:]}"
    )
