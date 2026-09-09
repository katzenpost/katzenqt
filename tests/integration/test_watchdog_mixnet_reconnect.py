"""Live confirmation of network.py's reconnect-triggered read watchdog,
targeting the actual scenario on_connection_status's is_connected reports:
the daemon's connectivity to the MIXNET (gateway), not the client's local
socket to the daemon process.

test_watchdog_reconnect.py bounces kpclientd itself (a full process
restart) and found empirically that on_connection_status never fires for
that: the thin_client library reconnects the local socket below the
callback layer, with no disconnected/reconnected transition surfaced to
the app. This test instead pauses the GATEWAY container -- kpclientd
keeps running and the client's local socket to it never drops, but the
daemon loses its route into the mixnet, which is exactly what the
pre-existing "daemon reports disconnected from mixnet; ARQ rides out and
retries" log line (on_connection_status, unchanged by this branch) is
about.

Touches only containers on the SAME compose network as the kpclientd
this session is configured against (see find_same_network_container),
so it can never affect another session's mixnet even when several are
running on this host. Skipped unless KATZENQT_DOCKER_INTEGRATION=1.
"""
from __future__ import annotations

import time

import pytest

from tests.integration._bounce_helpers import (
    bootstrap_voucher, spawn_role, run_role,
    find_kpclientd_container, find_same_network_container, podman,
    PhaseStopwatch,
)


def _poll_for(path, needle: str, deadline_s: float) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if needle in path.read_text():
            return True
        time.sleep(1.0)
    return False


@pytest.mark.integration
@pytest.mark.serial_docker
def test_read_recovers_promptly_after_mixnet_reconnect(
    kpclientd_endpoint, tmp_path_factory, monkeypatch,
):
    # INFO, not just the default WARNING: on_connection_status's
    # reconnected-transition line is logger.info.
    monkeypatch.setenv("KQT_LOG_LEVEL", "INFO")

    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("mixnet_reconnect_logs")
    bootstrap_voucher(alice_state, bob_state)
    kpclientd = find_kpclientd_container()
    gateway = find_same_network_container(kpclientd, "gateway1")

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"

    alice_proc = spawn_role(
        alice_state, "chat-session", "demo", "READ:m1:600",
        stdout_path=alice_out, stderr_path=alice_err,
    )

    gateway_paused = False
    tw = PhaseStopwatch("mixnet_reconnect")
    try:
        # Give Alice's read time to actually reach the daemon before we
        # sever its mixnet route.
        time.sleep(10.0)
        tw.mark("alice_read_registered")

        podman(["pause", gateway])
        gateway_paused = True
        saw_disconnect = _poll_for(alice_err, "reports disconnected from mixnet", 60.0)
        tw.mark("disconnect_seen")

        podman(["unpause", gateway])
        gateway_paused = False
        saw_reconnect = _poll_for(alice_err, "reports reconnected to mixnet", 90.0)
        tw.mark("reconnect_seen")

        # Bob's send happens only once we believe the daemon has
        # reconnected: Alice's read is still pending at that point (Bob
        # hasn't sent), so the reconnect_event is guaranteed to fire
        # before the read itself resolves.
        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=300.0)
        assert send.returncode == 0, send.stdout + send.stderr
        tw.mark("bob_sent")

        alice_proc.wait(timeout=300.0)
        tw.mark("alice_read")
    except Exception:
        alice_proc.kill()
        if gateway_paused:
            try:
                podman(["unpause", gateway])
            except Exception:
                pass
        raise

    alice_all = alice_out.read_text() + alice_err.read_text()
    for line in alice_all.splitlines():
        if any(t in line for t in (
            "STEP_OK", "STEP_FAIL", "SESSION_DONE", "mixnet",
            "reconnected mid-wait",
        )):
            print(f"[mixnet-reconnect] {line}")

    assert saw_disconnect, (
        "on_connection_status never reported the mixnet disconnect within 60s "
        f"of pausing the gateway\n{alice_err.read_text()[-6000:]}"
    )
    assert saw_reconnect, (
        "on_connection_status never reported the mixnet reconnect within 90s "
        f"of unpausing the gateway\n{alice_err.read_text()[-6000:]}"
    )
    assert alice_proc.returncode == 0, (
        f"alice chat-session failed rc={alice_proc.returncode}\n{alice_err.read_text()[-4000:]}"
    )
    assert "STEP_OK:0:READ:m1" in alice_all, (
        f"alice never read m1 after the mixnet reconnect\n{alice_err.read_text()[-6000:]}"
    )
    # The direct confirmation: the reconnect-generation detection in
    # _await_read_reply actually engaged for this read.
    assert "daemon reconnected mid-wait for bacap_stream=" in alice_all, (
        "the reconnect-triggered watchdog path never engaged for this read "
        "despite a confirmed mixnet disconnect/reconnect cycle\n"
        f"{alice_err.read_text()[-6000:]}"
    )
