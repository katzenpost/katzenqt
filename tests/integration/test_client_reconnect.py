"""Re-test the original wedge scenario end-to-end from the client side: a
message whose write is committed while the client is not on the wire must
ride out the disconnect and land once the client reconnects.

The original version (``test_write_survives_kpclientd_restart``) bounced the
kpclientd container with podman. That is unworkable for an integration suite
that must be able to run against a mixnet it does not own, so this version
triggers the outage entirely client-side:

- Bob commits ``m1`` to his local MixWAL and is killed mid-send — after the
  ``STEP_WAITING_ACK`` marker is logged (row committed, SentLog ACK still
  pending) but before ``wait_for_sent`` returns — then restarts from the same
  state dir. The thinclient reconnects with the same app ID (the daemon
  preserves per-app session state across a raw socket close) and the drain
  completes the write.
- Alice reads ``m1`` end-to-end.

Deterministic shape:

- Alice runs a long-lived chat-session that READs ``m1`` with the standard
  1500 s budget, which keeps her alive across the bounce AND polls through
  it: she starts reading before the outage and her read simply stays
  pending until the leftover write lands.
- Bob runs three short incarnations from ONE state dir:
  1. ``SEND:m0`` — baseline, proves the pair is connected and working;
  2. ``SEND:m1`` — killed the moment ``STEP_WAITING_ACK`` is logged. At that
     instant the row is committed but unsent, or sent-but-unacked; asserting
     he never reaches ``STEP_OK:...:SEND:m1`` before the kill makes the
     test fail loudly rather than silently degrade if the ACK ever becomes
     faster than the kill;
  3. a reconnect session that SLEEPs while the drain sweeps the leftover
     write-MixWAL row and delivers it. Measured: m1 lands ~one epoch after
     the bounce (epoch-aligned re-delivery), so the sleep is sized to the
     mixnet's actual epoch_duration (_bounce_helpers.epoch_duration_s()) to
     keep a live writer in the session across that boundary -- an earlier
     exit would leave no one riding m1 out;
- Alice's ``STEP_OK:0:READ:m1`` is the end-to-end proof the write rode out
  the disconnect and reconnect. She starts reading immediately (no leading
  SLEEP step), so her read is already in flight across the whole bounce —
  a strictly stronger scenario than reading only after it is over.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tests.integration._bounce_helpers import (
    run_role as _run_role,
    spawn_role as _spawn_role,
    bootstrap_voucher as _bootstrap_voucher,
    epoch_duration_s,
    PhaseStopwatch,
)


def _wait_for_token(out_path: Path, token: str, deadline_s: float, what: str) -> None:
    """Poll a spawned role's stderr file until a line containing token is
    written, mirroring the old test's STEP_OK polling. Deadlines are seconds.
    """
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if token in out_path.read_text():
            return
        time.sleep(0.5)
    raise AssertionError(
        f"{what}: no line containing {token!r} within {deadline_s:.0f}s\n"
        f"stderr tail:\n{out_path.read_text()[-3000:]}"
    )


def _terminate(proc: subprocess.Popen, what: str, *, expect_signal: bool = True) -> None:
    """Kill a spawned role, with a hard SIGKILL fallback so a stuck Python
    subprocess can't hang the test. ``headless.cli`` installs a SIGTERM
    handler that stops the event loop, so the raw SIGTERM exit is rc=-15
    (handler not run) OR rc=1 (loop stopped mid-``run_until_complete``);
    ``expect_signal`` asserts no other exit code, so a role that died on
    its own before the kill is called out instead of masked. ``_terminate``
    from a cleanup path uses the default so reaping an already-dead
    process can never mask the exception being handled."""
    proc.terminate()
    try:
        proc.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)
    if expect_signal and proc.returncode not in (-15, 1):
        raise AssertionError(
            f"{what} terminated with unexpected rc={proc.returncode}"
        )


@pytest.mark.integration
def test_write_survives_client_reconnect(kpclientd_endpoint, tmp_path_factory):
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("reconnect_logs")
    _bootstrap_voucher(alice_state, bob_state)

    # 1. Baseline: Bob's send is ACKed -> the pair is connected and working.
    baseline = _run_role(bob_state, "chat-session", "demo", "SEND:m0", timeout=300.0)
    assert "STEP_OK:0:SEND:m0" in baseline.stdout + baseline.stderr, (
        f"baseline SEND:m0 did not complete\n"
        f"stdout:\n{baseline.stdout}\nstderr:\n{baseline.stderr}"
    )

    tw = PhaseStopwatch("reconnect")
    alice_out = log_dir / "alice.out"
    alice_err = log_dir / "alice.err"
    bob2_out = log_dir / "bob2.out"
    bob2_err = log_dir / "bob2.err"

    alice_proc = _spawn_role(
        alice_state, "chat-session", "demo", "READ:m1:1500",
        stdout_path=alice_out, stderr_path=alice_err,
    )

    bob2_proc = None
    try:
        # 2. Bob commits m1 to MixWAL and is killed mid-send.
        tw.mark("spawn_alice")
        bob2_proc = _spawn_role(
            bob_state, "chat-session", "demo", "SEND:m1",
            stdout_path=bob2_out, stderr_path=bob2_err,
        )
        # STEP_WAITING_ACK is logged right after the MixWAL commit, before
        # wait_for_sent: at that instant the row is committed but unsent, or
        # sent-but-unacked. Either way it must ride out a reconnect.
        _wait_for_token(
            bob2_err, "STEP_WAITING_ACK:0:SEND:m1", 300.0,
            what="bob2 never committed SEND:m1 to MixWAL",
        )
        tw.mark("bob2_commit")
        # Must be alive at the kill point: bob2 dying on its own before the
        # SIGTERM would mean the write did not ride out a kill+reconnect at
        # all (and could never have been retried by a later incarnation).
        assert bob2_proc.poll() is None, (
            "bob2 died before the kill: m1 was not interrupted mid-send\n"
            f"bob2 stderr:\n{bob2_err.read_text()[-3000:]}"
        )
        _terminate(bob2_proc, "bob2")
        assert "STEP_OK:0:SEND:m1" not in bob2_err.read_text(), (
            "bob2 ACKed SEND:m1 before the kill: the write never rode out a"
            " disconnect (the race must fail loudly, not degrade silently)\n"
            f"bob2 stderr:\n{bob2_err.read_text()[-3000:]}"
        )
        tw.mark("bob2_killed")

        # 3. Reconnect session from the same state: the drain sweeps the
        # leftover write-MixWAL row and delivers m1. Measured: m1 lands ~one
        # epoch after the bounce (epoch-aligned re-delivery), so bob3 must
        # stay alive that long -- an earlier exit would leave no live writer
        # to ride m1 across the boundary. The subprocess timeout keeps the
        # same 5x margin over the sleep that was measured comfortable at
        # the docker mixnet's 2m epoch (120s sleep, 600s timeout).
        bob3_sleep_s = epoch_duration_s()
        bob3 = _run_role(
            bob_state, "chat-session", "demo", f"SLEEP:{bob3_sleep_s:.0f}",
            timeout=bob3_sleep_s * 5.0,
        )
        bob3_all = bob3.stdout + bob3.stderr
        for line in bob3_all.splitlines():
            if any(t in line for t in ("STEP_OK", "STEP_FAIL", "SESSION_DONE")):
                print(f"[reconnect][bob3] {line}")
        assert bob3.returncode == 0, (
            f"bob reconnect session failed rc={bob3.returncode}\n"
            f"stdout tail:\n{bob3.stdout[-3000:]}\nstderr tail:\n{bob3.stderr[-3000:]}"
        )
        assert "SESSION_DONE" in bob3_all, (
            f"bob reconnect session did not emit SESSION_DONE\n"
            f"stderr:\n{bob3.stderr[-3000:]}"
        )
        tw.mark("bob3_done")

        alice_proc.wait(timeout=2100.0)
        tw.mark("alice_done")
    except Exception:
        alice_proc.kill()
        if bob2_proc is not None:
            _terminate(bob2_proc, "bob2", expect_signal=False)
        raise

    alice_all = alice_out.read_text() + alice_err.read_text()
    for line in alice_all.splitlines():
        if any(t in line for t in ("STEP_OK", "STEP_FAIL", "STEP_POLL", "SESSION_DONE")):
            print(f"[reconnect][alice] {line}")

    assert alice_proc.returncode == 0, (
        f"alice chat-session failed rc={alice_proc.returncode}\n"
        f"stderr:\n{alice_err.read_text()[-3000:]}"
    )
    # The write that rode out the client disconnect+reconnect must have landed.
    assert "STEP_OK:0:READ:m1" in alice_all, (
        f"alice never read m1 after the reconnect\n"
        f"alice stderr:\n{alice_err.read_text()[-6000:]}"
    )
    assert "SESSION_DONE" in alice_all, (
        f"alice did not emit SESSION_DONE\n"
        f"alice stderr:\n{alice_err.read_text()[-3000:]}"
    )