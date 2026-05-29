"""Reproduce the restart bug: after the first Alice→Bob message exchange,
quit (separate process), then send + read again in fresh processes.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable


def _run_role(role_state: Path, *cli_args: str, timeout: float = 300.0):
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args]
    return subprocess.run(
        cmd, env=env, cwd=str(_REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _spawn_role(role_state: Path, *cli_args: str, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    """Popen variant for long-running chat-session subprocesses that we
    want running in parallel. We redirect stdout/stderr to files instead
    of pipes to avoid the classic 64 KB pipe-buffer deadlock: when one
    subprocess fills its stdout pipe, it blocks on write, and if the
    parent is `communicate`-ing a different subprocess, the blocked one
    can starve long enough for its background read loop to stall.
    """
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    cmd = [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args]
    return subprocess.Popen(
        cmd, env=env, cwd=str(_REPO_ROOT),
        stdout=open(stdout_path, "w"),
        stderr=open(stderr_path, "w"),
        text=True,
    )


def _expect_prefix(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"no prefix {prefix!r} in:\n{stdout}")


def _run_concurrent_session(
    alice_state: Path, bob_state: Path,
    alice_steps: list, bob_steps: list,
    *, round_label: str,
    process_timeout_s: float = 2400.0,
    log_dir: Path,
) -> None:
    """Run Alice and Bob as two concurrent chat-session subprocesses,
    each doing `alice_steps` / `bob_steps` in ONE process, and wait for
    them both to shut down cleanly.

    Asserts both subprocesses emit a final `SESSION_DONE` line, which is
    how _action_chat_session signals a clean shutdown after all steps
    ran. Emits each STEP_OK line to stdout so pytest -s surfaces the
    ordering in the log.
    """
    alice_out_path = log_dir / f"alice.{round_label}.out"
    alice_err_path = log_dir / f"alice.{round_label}.err"
    bob_out_path = log_dir / f"bob.{round_label}.out"
    bob_err_path = log_dir / f"bob.{round_label}.err"

    alice_proc = _spawn_role(
        alice_state, "chat-session", "demo", *alice_steps,
        stdout_path=alice_out_path, stderr_path=alice_err_path,
    )
    bob_proc = _spawn_role(
        bob_state, "chat-session", "demo", *bob_steps,
        stdout_path=bob_out_path, stderr_path=bob_err_path,
    )

    try:
        alice_proc.wait(timeout=process_timeout_s)
        bob_proc.wait(timeout=process_timeout_s)
    except subprocess.TimeoutExpired:
        alice_proc.kill()
        bob_proc.kill()
        raise

    alice_out = alice_out_path.read_text()
    alice_err = alice_err_path.read_text()
    bob_out = bob_out_path.read_text()
    bob_err = bob_err_path.read_text()

    # Surface per-step progress into the pytest log so a failure in
    # either side is locatable.
    for who, out in (("alice", alice_out), ("bob", bob_out)):
        for line in out.splitlines():
            if (
                line.startswith("STEP_OK")
                or line.startswith("STEP_FAIL")
                or line.startswith("STEP_POLL")
                or line == "SESSION_DONE"
            ):
                print(f"[{round_label}][{who}] {line}")

    assert alice_proc.returncode == 0, (
        f"[{round_label}] alice chat-session failed rc={alice_proc.returncode}\n"
        f"stdout tail:\n{alice_out[-3000:]}\nstderr tail:\n{alice_err[-3000:]}"
    )
    assert bob_proc.returncode == 0, (
        f"[{round_label}] bob chat-session failed rc={bob_proc.returncode}\n"
        f"stdout tail:\n{bob_out[-3000:]}\nstderr tail:\n{bob_err[-3000:]}"
    )
    assert "SESSION_DONE" in alice_out, (
        f"[{round_label}] alice did not emit SESSION_DONE:\n{alice_out[-3000:]}"
    )
    assert "SESSION_DONE" in bob_out, (
        f"[{round_label}] bob did not emit SESSION_DONE:\n{bob_out[-3000:]}"
    )


@pytest.mark.integration
def test_concurrent_session_shutdown_then_restart(kpclientd_endpoint, tmp_path_factory):
    """Critical bug-hunting test: Alice and Bob each run as a single
    long-lived subprocess (not one subprocess per step), exchange
    messages in BOTH directions, shut down cleanly, and then a NEW pair
    of subprocesses starts back up from their state files and does
    another bidirectional exchange.

    This is the scenario the user reports: two running clients, both
    quit, both restart from disk — if the state on disk is not
    correctly saved or not correctly reloaded, round 2 will fail.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # --- Setup: create conv on both sides and exchange invites.
    create_a = _run_role(alice_state, "create-conv", "demo", "alice", timeout=180.0)
    assert create_a.returncode == 0, create_a.stdout + create_a.stderr
    invite_a = _expect_prefix(create_a.stdout, "INVITE=")

    create_b = _run_role(bob_state, "create-conv", "demo", "bob", timeout=180.0)
    assert create_b.returncode == 0, create_b.stdout + create_b.stderr
    invite_b = _expect_prefix(create_b.stdout, "INVITE=")

    accept_b = _run_role(bob_state, "accept-invite", "demo", "bob", "alice", invite_a, timeout=30.0)
    assert accept_b.returncode == 0
    accept_a = _run_role(alice_state, "accept-invite", "demo", "alice", "bob", invite_b, timeout=30.0)
    assert accept_a.returncode == 0

    # --- Round 1: concurrent session. Each side sends one and reads one;
    # a single bidirectional exchange is enough to prove the round works,
    # and multi-message-per-session is covered by
    # test_multi_send_then_restart_read. The SLEEP at the end gives the
    # background read loop a beat to drain the last message before we shut
    # down (so no PWAL/MixWAL is mid-flight at quit time; reloading stale
    # in-flight entries is a separate concern).
    alice_steps_r1 = [
        "SEND:a-r1-msg1",
        "READ:b-r1-msg1",
        "SLEEP:2",
    ]
    bob_steps_r1 = [
        "SEND:b-r1-msg1",
        "READ:a-r1-msg1",
        "SLEEP:2",
    ]
    log_dir = tmp_path_factory.mktemp("concurrent_logs")
    _run_concurrent_session(
        alice_state, bob_state, alice_steps_r1, bob_steps_r1,
        round_label="round1", log_dir=log_dir,
    )

    # --- Shutdown confirmed (both emitted SESSION_DONE). State is on
    # disk. Now a FRESH pair of subprocesses must continue the chat.

    # --- Round 2: restart and keep chatting. If saving/loading is
    # broken, one of these reads will time out.
    alice_steps_r2 = [
        "SEND:a-r2-msg1",
        "READ:b-r2-msg1",
    ]
    bob_steps_r2 = [
        "SEND:b-r2-msg1",
        "READ:a-r2-msg1",
    ]
    _run_concurrent_session(
        alice_state, bob_state, alice_steps_r2, bob_steps_r2,
        round_label="round2", log_dir=log_dir,
    )


@pytest.mark.integration
def test_multi_send_then_restart_read(kpclientd_endpoint, tmp_path_factory):
    """Alice queues 3 messages in one subprocess, then quits. Bob is then
    started fresh and must read all 3. Emulates 'user typed fast, then
    quit, peer came online later'.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    create = _run_role(alice_state, "create-conv", "demo", "alice", timeout=180.0)
    assert create.returncode == 0, create.stdout + create.stderr
    invite = _expect_prefix(create.stdout, "INVITE=")

    accept = _run_role(bob_state, "accept-invite", "demo", "bob", "alice", invite, timeout=30.0)
    assert accept.returncode == 0

    send = _run_role(
        alice_state, "multi-send", "demo", "m1|m2|m3", timeout=600.0,
    )
    assert send.returncode == 0 and "SENT" in send.stdout, send.stdout + send.stderr

    # Bob restarts fresh and must receive all three in order.
    for expected in ("m1", "m2", "m3"):
        r = _run_role(bob_state, "read", "demo", "360", expected, timeout=400.0)
        assert r.returncode == 0, (
            f"bob failed to read {expected!r}:\n"
            f"stdout tail:\n{r.stdout[-3000:]}\nstderr tail:\n{r.stderr[-3000:]}"
        )
        print(f"[multi] bob received {expected}")


@pytest.mark.integration
def test_read_latency_after_continuous_peer_sends(kpclientd_endpoint, tmp_path_factory):
    """Measure end-to-end latency from Bob's send completion to Alice's
    ConvLog commit, over several back-to-back messages.

    Bob sends 5 messages in a single long-lived session. Alice runs her
    own long-lived session that simply READs each of them in turn and
    timestamps the observation. Since both STEP_OK lines carry ts=,
    we can compute per-message gap "bob SENT ts" - "alice RECV ts".

    Generous bounds are asserted on the latency: mean gap < 120s and
    per-message gap < 240s. Observed values on a healthy local docker
    mixnet sit around 20s mean / 25s max, so these limits exist mostly
    to catch the failure mode where alice silently never reads — the
    timestamps in the pytest log remain the actual diagnostic.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"
    log_dir = tmp_path_factory.mktemp("latency_logs")

    create_a = _run_role(alice_state, "create-conv", "demo", "alice", timeout=180.0)
    assert create_a.returncode == 0, create_a.stdout + create_a.stderr
    invite_a = _expect_prefix(create_a.stdout, "INVITE=")

    create_b = _run_role(bob_state, "create-conv", "demo", "bob", timeout=180.0)
    assert create_b.returncode == 0
    invite_b = _expect_prefix(create_b.stdout, "INVITE=")

    # Both sides accept each other's invite so each has an active peer
    # reading the other's stream.
    accept_b = _run_role(bob_state, "accept-invite", "demo", "bob", "alice", invite_a, timeout=30.0)
    assert accept_b.returncode == 0
    accept_a = _run_role(alice_state, "accept-invite", "demo", "alice", "bob", invite_b, timeout=30.0)
    assert accept_a.returncode == 0

    n = 5
    bob_steps = []
    alice_steps = []
    for i in range(n):
        bob_steps.append(f"SEND:m{i}")
        alice_steps.append(f"READ:m{i}")
    bob_proc = _spawn_role(
        bob_state, "chat-session", "demo", *bob_steps,
        stdout_path=log_dir / "bob.out", stderr_path=log_dir / "bob.err",
    )
    alice_proc = _spawn_role(
        alice_state, "chat-session", "demo", *alice_steps,
        stdout_path=log_dir / "alice.out", stderr_path=log_dir / "alice.err",
    )
    try:
        bob_proc.wait(timeout=1200.0)
        alice_proc.wait(timeout=1200.0)
    except subprocess.TimeoutExpired:
        bob_proc.kill()
        alice_proc.kill()
        raise

    bob_out = (log_dir / "bob.out").read_text()
    alice_out = (log_dir / "alice.out").read_text()

    import re
    send_ts = {}  # text -> ts
    for line in bob_out.splitlines():
        m = re.match(r"^STEP_OK:\d+:SEND:(m\d+):ts=(\d+\.\d+)$", line)
        if m:
            send_ts[m.group(1)] = float(m.group(2))
    recv_ts = {}
    for line in alice_out.splitlines():
        m = re.match(r"^STEP_OK:\d+:READ:(m\d+):ts=(\d+\.\d+)$", line)
        if m:
            recv_ts[m.group(1)] = float(m.group(2))

    print(f"[latency] bob sent {len(send_ts)} messages, alice received {len(recv_ts)}")
    assert len(send_ts) == n, f"bob didn't complete all sends: {send_ts}\n---\n{bob_out[-2000:]}"
    assert len(recv_ts) == n, f"alice didn't receive all messages: {recv_ts}\n---\n{alice_out[-2000:]}"

    gaps = []
    for i in range(n):
        key = f"m{i}"
        gap = recv_ts[key] - send_ts[key]
        gaps.append(gap)
        print(f"[latency] {key}: bob SEND_ACK={send_ts[key]:.3f} alice OBSERVED={recv_ts[key]:.3f} gap={gap:+.2f}s")

    mean_gap = sum(gaps) / len(gaps)
    max_gap = max(gaps)
    print(f"[latency] gap stats: min={min(gaps):.2f}s max={max_gap:.2f}s "
          f"mean={mean_gap:.2f}s")
    assert bob_proc.returncode == 0
    assert alice_proc.returncode == 0
    assert mean_gap < 120.0, (
        f"bob->alice mean read latency {mean_gap:.1f}s exceeds 120s ceiling; "
        f"per-message gaps={[f'{g:.1f}' for g in gaps]}"
    )
    assert max_gap < 240.0, (
        f"bob->alice per-message read latency {max_gap:.1f}s exceeds 240s ceiling; "
        f"per-message gaps={[f'{g:.1f}' for g in gaps]}"
    )


@pytest.mark.integration
def test_bidirectional_restart(kpclientd_endpoint, tmp_path_factory):
    """Alice and Bob both invite each other (bidirectional). They exchange
    messages in round 1, both quit. In round 2 (fresh subprocesses) Alice
    sends msg2A and Bob must read it; Bob sends msg2B and Alice must read it.

    This matches the user-reported scenario: 'alice and bob can invite each
    other to a group chat and chat with each other, but after restart they
    can no longer read each other's messages'.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # Both create a conversation named "demo" on each side.
    create_a = _run_role(alice_state, "create-conv", "demo", "alice", timeout=180.0)
    assert create_a.returncode == 0, create_a.stdout + create_a.stderr
    invite_a = _expect_prefix(create_a.stdout, "INVITE=")
    print(f"[setup] alice invite: {invite_a[:32]}...")

    create_b = _run_role(bob_state, "create-conv", "demo", "bob", timeout=180.0)
    assert create_b.returncode == 0, create_b.stdout + create_b.stderr
    invite_b = _expect_prefix(create_b.stdout, "INVITE=")
    print(f"[setup] bob invite: {invite_b[:32]}...")

    # Bob accepts Alice's invite (into his 'demo' conv).
    accept_b = _run_role(bob_state, "accept-invite", "demo", "bob", "alice", invite_a, timeout=30.0)
    assert accept_b.returncode == 0, accept_b.stdout + accept_b.stderr

    # Alice accepts Bob's invite (into her 'demo' conv).
    accept_a = _run_role(alice_state, "accept-invite", "demo", "alice", "bob", invite_b, timeout=30.0)
    assert accept_a.returncode == 0, accept_a.stdout + accept_a.stderr

    # Round 1: each sends one message, the other reads.
    s1a = _run_role(alice_state, "send", "demo", "hello-from-alice", timeout=300.0)
    assert s1a.returncode == 0 and "SENT" in s1a.stdout, s1a.stdout + s1a.stderr

    s1b = _run_role(bob_state, "send", "demo", "hello-from-bob", timeout=300.0)
    assert s1b.returncode == 0 and "SENT" in s1b.stdout, s1b.stdout + s1b.stderr

    r1b = _run_role(bob_state, "read", "demo", "360", "hello-from-alice", timeout=400.0)
    assert r1b.returncode == 0, f"bob read1 failed:\n{r1b.stdout}\n{r1b.stderr}"

    r1a = _run_role(alice_state, "read", "demo", "360", "hello-from-bob", timeout=400.0)
    assert r1a.returncode == 0, f"alice read1 failed:\n{r1a.stdout}\n{r1a.stderr}"
    print("[r1] bidirectional exchange complete")

    # Round 2 — restart scenario. Fresh subprocesses, state loaded from disk.
    s2a = _run_role(alice_state, "send", "demo", "round2-from-alice", timeout=300.0)
    assert s2a.returncode == 0 and "SENT" in s2a.stdout, s2a.stdout + s2a.stderr

    s2b = _run_role(bob_state, "send", "demo", "round2-from-bob", timeout=300.0)
    assert s2b.returncode == 0 and "SENT" in s2b.stdout, s2b.stdout + s2b.stderr

    r2b = _run_role(bob_state, "read", "demo", "360", "round2-from-alice", timeout=400.0)
    print(f"[r2] bob read2 stdout tail:\n{r2b.stdout[-2000:]}\nstderr tail:\n{r2b.stderr[-3000:]}")
    assert r2b.returncode == 0, "bob read2 did not find round2-from-alice"

    r2a = _run_role(alice_state, "read", "demo", "360", "round2-from-bob", timeout=400.0)
    print(f"[r2] alice read2 stdout tail:\n{r2a.stdout[-2000:]}\nstderr tail:\n{r2a.stderr[-3000:]}")
    assert r2a.returncode == 0, "alice read2 did not find round2-from-bob"
