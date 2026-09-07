"""Live confirmation of the PKI-epoch-rollover fix in
drain_mixwal_read_single: a read that spans an epoch rollover before its
peer ever sends anything must still complete promptly once they do,
rather than hanging on a stale envelope (PR45 finding #5; see
network.py's on_new_pki_document and _await_read_reply).

Relies on the docker mixnet's short epoch_duration (2m by default) to
observe a real rollover within a reasonable test time. Touches no
containers at all -- purely a timing scenario -- so it's safe to run
alongside the other integration files, though it's still slow enough
(one full epoch's wait) to run on its own.

Skipped unless KATZENQT_DOCKER_INTEGRATION=1 (see conftest.py).
"""
from __future__ import annotations

import time

import pytest

from tests.integration._bounce_helpers import bootstrap_voucher, spawn_role, run_role


@pytest.mark.integration
def test_read_recovers_after_epoch_rollover(kpclientd_endpoint, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("KQT_LOG_LEVEL", "INFO")

    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("epoch_rollover_logs")
    bootstrap_voucher(alice_state, bob_state)

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"

    # Alice waits for a message Bob hasn't sent yet: a genuine in-flight
    # read whose envelope will still be sitting there when the epoch rolls.
    alice_proc = spawn_role(
        alice_state, "chat-session", "demo", "READ:m1:600",
        stdout_path=alice_out, stderr_path=alice_err,
    )

    try:
        # Comfortably more than one docker-mixnet epoch (2m default), so
        # Alice's read is guaranteed to have spanned at least one rollover
        # before Bob ever sends anything.
        time.sleep(140.0)

        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=300.0)
        assert send.returncode == 0, send.stdout + send.stderr

        # If the fix regressed, this hangs on the stale envelope up to the
        # 1200s backstop; bound the wait well under that so a regression
        # fails the test instead of stalling the suite for 20 minutes.
        alice_proc.wait(timeout=180.0)
    except Exception:
        alice_proc.kill()
        raise

    alice_all = alice_out.read_text() + alice_err.read_text()
    for line in alice_all.splitlines():
        if any(t in line for t in (
            "STEP_OK", "STEP_FAIL", "SESSION_DONE", "epoch", "Epoch",
        )):
            print(f"[epoch-rollover] {line}")

    assert alice_proc.returncode == 0, (
        f"alice chat-session failed rc={alice_proc.returncode}\n{alice_err.read_text()[-4000:]}"
    )
    assert "STEP_OK:0:READ:m1" in alice_all, (
        f"alice never read m1 after the epoch rollover\n{alice_err.read_text()[-6000:]}"
    )
    # Alice's box doesn't exist until Bob's send below, so her read stays
    # pending for the entire 140s sleep -- longer than one docker-mixnet
    # epoch (120s) -- guaranteeing a rollover happened while it was
    # in-flight. This is the direct confirmation on_new_pki_document fired
    # and _await_read_reply's epoch-triggered grace path actually engaged.
    assert "PKI epoch rolled over mid-wait for bacap_stream=" in alice_all, (
        "the epoch-rollover watchdog path never engaged despite the read "
        "spanning a full epoch_duration; either it recovered some other "
        "way, or the fix regressed\n"
        f"{alice_err.read_text()[-6000:]}"
    )
