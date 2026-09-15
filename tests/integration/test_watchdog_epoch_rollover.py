"""Live confirmation of the PKI-epoch-rollover fix in
drain_mixwal_read_single: a read that spans an epoch rollover before its
peer ever sends anything must still complete promptly once they do,
rather than hanging on a stale envelope (PR45 finding #5; see
network.py's on_new_pki_document and _await_read_reply).

The read re-polls locally instead of parking in the daemon's ride-out,
so no single attempt need span a rollover and the epoch grace log is no
longer reliable here; test_network_fake.py covers that path.

Relies on the docker mixnet's short epoch_duration (2m by default) to
observe a real rollover within a reasonable test time. Touches no
containers at all -- purely a timing scenario -- so it's safe to run
alongside the other integration files, though it's still slow enough
(one full epoch's wait) to run on its own.

Skipped unless KATZENQT_DOCKER_INTEGRATION=1 (see conftest.py).
"""
from __future__ import annotations

import re

import time

import pytest

from tests.integration._bounce_helpers import (
    bootstrap_voucher, epoch_duration_s, run_role, spawn_role,
)

# Emitted by network.on_new_pki_document on every epoch advance, whatever
# is in flight, so the poll below and the assertion cannot drift apart.
_EPOCH_ADVANCE_RE = re.compile(r"PKI epoch advanced to \d+")


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
        # Wait for a real epoch boundary to pass while Alice's read is
        # outstanding, rather than sleeping a fixed span and hoping it
        # covered one. Bounded at two epochs plus margin so a mixnet that
        # never advances fails here instead of later.
        deadline = time.time() + 2 * epoch_duration_s() + 30.0
        while time.time() < deadline:
            if _EPOCH_ADVANCE_RE.search(alice_err.read_text()):
                break
            time.sleep(1.0)
        else:
            raise AssertionError(
                "no PKI epoch advance observed while alice's read was "
                f"outstanding\n{alice_err.read_text()[-4000:]}"
            )

        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=750.0)
        assert send.returncode == 0, send.stdout + send.stderr

        # If the fix regressed, this hangs on the stale envelope up to the
        # 1200s backstop; bound the wait well under that so a regression
        # fails the test instead of stalling the suite for 20 minutes.
        alice_proc.wait(timeout=450.0)
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
    # Alice's read spans the 140s sleep; assert a rollover really happened
    # rather than trusting that it still exceeds epoch_duration.
    assert _EPOCH_ADVANCE_RE.search(alice_all), (
        "no epoch advance during alice's read, so no rollover was exercised\n"
        f"{alice_err.read_text()[-6000:]}"
    )
