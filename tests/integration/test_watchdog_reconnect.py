"""Live confirmation that a pending read survives a full kpclientd process
restart (podman stop/start), not just a mixnet-level connectivity blip.

This was originally written to confirm the reconnect-triggered read
watchdog (_await_read_reply racing on_connection_status's
_reconnect_event); empirically it does NOT exercise that path. A full
daemon restart never fires on_connection_status at all here -- the
thin_client library reconnects the client's local socket to the new
daemon process below the callback layer, with no disconnected/reconnected
transition surfaced to the app (only its own "Attempting to reconnect to
daemon" debug line). See test_watchdog_mixnet_reconnect.py for the
scenario that DOES exercise on_connection_status and the watchdog's
grace-period path: pausing the gateway so the daemon process keeps
running but loses its mixnet route, which is what on_connection_status's
pre-existing "disconnected from mixnet" line is actually about.

What this test still confirms, and is worth keeping for: the read-drain
loop (give_up()/OSError handling/prompt-retry fixes in this branch) does
not hang or crash across a full daemon restart, recovering via its
pre-existing exception-handling paths.

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
        f"alice never read m1 after the restart\n{alice_err.read_text()[-6000:]}"
    )
    assert "SESSION_DONE" in alice_all, (
        f"clean-shutdown sentinel missing\n{alice_err.read_text()[-3000:]}"
    )
    # NOT asserted here: "daemon reconnected mid-wait for bacap_stream=".
    # A full daemon restart doesn't fire on_connection_status at all (see
    # the module docstring), so the read recovers via the pre-existing
    # exception-handling paths, not the reconnect watchdog. That path is
    # confirmed separately in test_watchdog_mixnet_reconnect.py.
