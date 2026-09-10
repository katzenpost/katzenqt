"""Live confirmation of the PKI-epoch-rollover fix in
drain_mixwal_read_single: a read that spans an epoch rollover before its
peer ever sends anything must still complete promptly once they do,
rather than hanging on a stale envelope (PR45 finding #5; see
network.py's on_new_pki_document and _await_read_reply).

All waits below are sized off the mixnet's actual epoch_duration (see
_bounce_helpers.epoch_duration_s()) rather than the docker mixnet's 2m
default, so this test also works unmodified against a network with a
longer epoch -- just proportionally slower to run -- with one deliberate
exception: alice_proc's final wait() is a fixed fail-fast bound, not
epoch-derived (see its own comment below). Touches no containers at all
-- purely a timing scenario -- so it's safe to run alongside the other
integration files, though it's still slow enough (one full epoch's wait)
to run on its own.

Skipped unless KATZENQT_DOCKER_INTEGRATION=1 (see conftest.py).
"""
from __future__ import annotations

from pathlib import Path

import time

import pytest

from tests.integration._bounce_helpers import (
    bootstrap_voucher, spawn_role, run_role, epoch_duration_s, PhaseStopwatch,
)


# Emitted by network.py's on_new_pki_document when a read's epoch-triggered
# grace path engages. Shared between the poll below and the final assertion
# so the two can't silently drift apart if the log wording ever changes.
_ROLLOVER_LOG_NEEDLE = "PKI epoch rolled over mid-wait for bacap_stream="


def _poll_for(path: Path, needle: str, deadline_s: float) -> bool:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if needle in path.read_text():
            return True
        time.sleep(1.0)
    return False


@pytest.mark.integration
def test_read_recovers_after_epoch_rollover(kpclientd_endpoint, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("KQT_LOG_LEVEL", "INFO")

    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("epoch_rollover_logs")
    bootstrap_voucher(alice_state, bob_state)

    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"

    # Alice's own READ deadline must comfortably outlast the rollover poll
    # below plus the post-send read wait, or her chat-session would give up
    # before either has a chance to happen.
    read_deadline_s = epoch_duration_s() + 480.0
    rollover_poll_deadline_s = epoch_duration_s() + 100.0

    # Alice waits for a message Bob hasn't sent yet: a genuine in-flight
    # read whose envelope will still be sitting there when the epoch rolls.
    alice_proc = spawn_role(
        alice_state, "chat-session", "demo", f"READ:m1:{read_deadline_s:.0f}",
        stdout_path=alice_out, stderr_path=alice_err,
    )

    tw = PhaseStopwatch("epoch_rollover")
    try:
        # Wait for a real PKI epoch rollover to be observed mid-read, rather
        # than sleeping a fixed >1-epoch interval: the rollover fires at the
        # next epoch boundary (up to one epoch_duration away), so this is
        # typically far shorter than a blind 2x-epoch sleep and never longer
        # in the worst case. Alice's box doesn't exist yet (Bob hasn't sent),
        # so her read stays pending until the boundary genuinely rolls.
        if not _poll_for(alice_err, _ROLLOVER_LOG_NEEDLE, rollover_poll_deadline_s):
            raise AssertionError(
                f"PKI epoch did not roll over mid-wait within "
                f"{rollover_poll_deadline_s:.0f}s\n"
                f"{alice_err.read_text()[-4000:]}"
            )
        tw.mark("rollover_seen")

        send = run_role(bob_state, "chat-session", "demo", "SEND:m1", timeout=300.0)
        assert send.returncode == 0, send.stdout + send.stderr
        tw.mark("bob_sent")

        # If the fix regressed, this hangs on the stale envelope up to the
        # 1200s backstop; bound the wait well under that so a regression
        # fails the test instead of stalling the suite for 20 minutes.
        alice_proc.wait(timeout=180.0)
        tw.mark("alice_read")
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
    # pending until the _poll_for above observes a genuine epoch rollover.
    # This is the direct confirmation on_new_pki_document fired and
    # _await_read_reply's epoch-triggered grace path actually engaged.
    assert _ROLLOVER_LOG_NEEDLE in alice_all, (
        "the epoch-rollover watchdog path never engaged despite the read "
        "spanning a full epoch_duration; either it recovered some other "
        "way, or the fix regressed\n"
        f"{alice_err.read_text()[-6000:]}"
    )
