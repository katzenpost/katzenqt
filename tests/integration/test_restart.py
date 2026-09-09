"""Reproduce the restart bug: after the first Alice→Bob message exchange,
quit (separate process), then send + read again in fresh processes.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).
"""
from __future__ import annotations

import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from katzenqt import models
from tests.integration._bounce_helpers import (
    REPO_ROOT as _REPO_ROOT,
    PYTHON as _PYTHON,
    KP_ADDR as _KP_ADDR,
    CONN_ARGS as _CONN_ARGS,
    run_role as _run_role,
    spawn_role as _spawn_role,
    combined as _combined,
    expect_token as _expect_token,
    bootstrap_voucher as _bootstrap_voucher,
)


def _snapshot_role_state(state: Path, label: str) -> None:
    """Dump a read-only forensic snapshot of a role's state DB to the pytest
    log (``[snap][<label>]`` lines) so a failure can be classified against
    the read-side hypotheses:
      (a) leftover read-``MixWAL`` for the final message -> the read-drain
          strand died before sweeping it;
      (b) ``ReadCapWAL.next_index`` advanced past it -> an index-skip race;
      (c) neither -> the mixnet never delivered it (nondelivery).

    The DB file is copied first (with its ``-wal``/``-shm`` siblings) and
    the copy opened read-only, so this never contends with or perturbs a
    live process, and committed-but-uncheckpointed WAL data is still read.
    """
    src = Path(str(state) + ".sqlite3")
    if not src.is_file():
        print(f"[snap][{label}] no state db at {src}")
        return
    snap_dir = Path(tempfile.mkdtemp(prefix="kqt-snap-"))
    dst = snap_dir / "state.sqlite3"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        sibling = Path(str(src) + suffix)
        if sibling.is_file():
            shutil.copy2(sibling, f"{dst}{suffix}")
    try:
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        cur = conn.cursor()

        cur.execute(
            "SELECT cl.conversation_order, cl.conversation_peer_id,"
            " cl.network_status, cl.payload"
            " FROM conversationlog cl ORDER BY cl.conversation_order"
        )
        for order, peer_id, net_status, payload in cur.fetchall():
            text = ""
            if payload[:1] == b"F":
                try:
                    gcm = models.GroupChatMessage.from_cbor(payload[1:])
                    text = f" text={gcm.text!r} type={gcm.msg_type.name}"
                except Exception:
                    text = " (undecodable payload)"
            print(
                f"[snap][{label}] convlog order={order} peer={peer_id}"
                f" net={net_status}{text}"
            )

        cur.execute("SELECT is_read, count(*) FROM mixwal GROUP BY is_read")
        for is_read, n in cur.fetchall():
            print(f"[snap][{label}] mixwal count is_read={is_read}: {n}")
        cur.execute("SELECT id, bacap_stream FROM mixwal WHERE is_read=1")
        for rid, stream in cur.fetchall():
            print(f"[snap][{label}] leftover read-MixWAL id={rid} stream={stream}")

        cur.execute("SELECT id, next_index FROM readcapwal")
        for rid, ni in cur.fetchall():
            head = 0
            if ni:
                head = struct.unpack("<Q", ni[:8])[0]
            print(
                f"[snap][{label}] readcapwal stream={rid}"
                f" next_idx_head={head} len={len(ni) if ni else 0}"
            )

        for table in ("sentlog", "plaintextwal"):
            try:
                cur.execute(f"SELECT count(*) FROM {table}")
                print(f"[snap][{label}] {table} count: {cur.fetchone()[0]}")
            except sqlite3.OperationalError:
                print(f"[snap][{label}] {table}: no table")
        conn.close()
    finally:
        shutil.rmtree(snap_dir, ignore_errors=True)


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

    # chat-session emits its STEP_*/SESSION_DONE tokens through logging,
    # which the spawned process writes to its stderr file.
    alice_all = alice_out + alice_err
    bob_all = bob_out + bob_err

    # Surface per-step progress into the pytest log so a failure in
    # either side is locatable.
    for who, text in (("alice", alice_all), ("bob", bob_all)):
        for line in text.splitlines():
            if any(t in line for t in ("STEP_OK", "STEP_FAIL", "STEP_POLL", "SESSION_DONE")):
                print(f"[{round_label}][{who}] {line}")

    assert alice_proc.returncode == 0, (
        f"[{round_label}] alice chat-session failed rc={alice_proc.returncode}\n"
        f"stdout tail:\n{alice_out[-3000:]}\nstderr tail:\n{alice_err[-3000:]}"
    )
    assert bob_proc.returncode == 0, (
        f"[{round_label}] bob chat-session failed rc={bob_proc.returncode}\n"
        f"stdout tail:\n{bob_out[-3000:]}\nstderr tail:\n{bob_err[-3000:]}"
    )
    assert "SESSION_DONE" in alice_all, (
        f"[{round_label}] alice did not emit SESSION_DONE:\n{alice_err[-3000:]}"
    )
    assert "SESSION_DONE" in bob_all, (
        f"[{round_label}] bob did not emit SESSION_DONE:\n{bob_err[-3000:]}"
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

    NOTE: overlaps heavily with test_bidirectional_restart (the same
    bait: concurrent two-way exchange, clean shutdown, restart from
    disk, exchange again). Kept separate because pairing with the other
    restart tests here costs nothing once parallelism masks the wall
    clock; if the suite ever needs to shrink, the shared core of the
    two could be merged into one test.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # --- Setup: establish mutual contact via the Contact Voucher handshake.
    _bootstrap_voucher(alice_state, bob_state)

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
    """Alice queues 2 messages in one subprocess, then quits. Bob is then
    started fresh and must read both. Emulates 'user typed fast, then
    quit, peer came online later'.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    _bootstrap_voucher(alice_state, bob_state)

    send = _run_role(
        alice_state, "multi-send", "demo", "m1|m2", timeout=600.0,
    )
    assert send.returncode == 0 and "SENT" in _combined(send), send.stdout + send.stderr

    # Bob restarts fresh and must receive both in order.
    for expected in ("m1", "m2"):
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

    Bob sends 3 messages in a single long-lived session. Alice runs her
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

    _bootstrap_voucher(alice_state, bob_state)

    n = 3
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
        # Snapshot BEFORE re-raising: a timeout is exactly the "alice
        # silently never reads" case this diagnostic exists for, so it must
        # not be skipped on the one path it was written to classify.
        _snapshot_role_state(alice_state, "alice")
        _snapshot_role_state(bob_state, "bob")
        raise

    # Forensic snapshot AFTER both roles have exited so the WAL is settled:
    # classifies a failure as (a) leftover read-MixWAL, (b) ReadCapWAL index
    # skip, or (c) mixnet nondelivery.
    _snapshot_role_state(alice_state, "alice")
    _snapshot_role_state(bob_state, "bob")

    # STEP_OK tokens are logged to stderr (with a level/name prefix), so
    # combine both streams and match by search rather than anchored match.
    bob_out = (log_dir / "bob.out").read_text() + (log_dir / "bob.err").read_text()
    alice_out = (log_dir / "alice.out").read_text() + (log_dir / "alice.err").read_text()

    import re
    send_ts = {}  # text -> ts
    for line in bob_out.splitlines():
        m = re.search(r"STEP_OK:\d+:SEND:(m\d+):ts=(\d+\.\d+)", line)
        if m:
            send_ts[m.group(1)] = float(m.group(2))
    recv_ts = {}
    for line in alice_out.splitlines():
        m = re.search(r"STEP_OK:\d+:READ:(m\d+):ts=(\d+\.\d+)", line)
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

    NOTE: overlaps heavily with test_concurrent_session_shutdown_then_restart
    (the same bait: two-way exchange, clean shutdown, restart from disk,
    exchange again). Kept separate because pairing with the other restart
    tests here costs nothing once parallelism masks the wall clock; if
    the suite ever needs to shrink, the shared core of the two could be
    merged into one test.
    """
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # Establish mutual contact via the Contact Voucher handshake.
    _bootstrap_voucher(alice_state, bob_state)

    # Round 1: each sends one message, the other reads.
    s1a = _run_role(alice_state, "send", "demo", "hello-from-alice", timeout=300.0)
    assert s1a.returncode == 0 and "SENT" in _combined(s1a), s1a.stdout + s1a.stderr

    s1b = _run_role(bob_state, "send", "demo", "hello-from-bob", timeout=300.0)
    assert s1b.returncode == 0 and "SENT" in _combined(s1b), s1b.stdout + s1b.stderr

    r1b = _run_role(bob_state, "read", "demo", "360", "hello-from-alice", timeout=400.0)
    assert r1b.returncode == 0, f"bob read1 failed:\n{r1b.stdout}\n{r1b.stderr}"

    r1a = _run_role(alice_state, "read", "demo", "360", "hello-from-bob", timeout=400.0)
    assert r1a.returncode == 0, f"alice read1 failed:\n{r1a.stdout}\n{r1a.stderr}"
    print("[r1] bidirectional exchange complete")

    # Round 2 — restart scenario. Fresh subprocesses, state loaded from disk.
    s2a = _run_role(alice_state, "send", "demo", "round2-from-alice", timeout=300.0)
    assert s2a.returncode == 0 and "SENT" in _combined(s2a), s2a.stdout + s2a.stderr

    s2b = _run_role(bob_state, "send", "demo", "round2-from-bob", timeout=300.0)
    assert s2b.returncode == 0 and "SENT" in _combined(s2b), s2b.stdout + s2b.stderr

    r2b = _run_role(bob_state, "read", "demo", "360", "round2-from-alice", timeout=400.0)
    print(f"[r2] bob read2 stdout tail:\n{r2b.stdout[-2000:]}\nstderr tail:\n{r2b.stderr[-3000:]}")
    assert r2b.returncode == 0, "bob read2 did not find round2-from-alice"

    r2a = _run_role(alice_state, "read", "demo", "360", "round2-from-bob", timeout=400.0)
    print(f"[r2] alice read2 stdout tail:\n{r2a.stdout[-2000:]}\nstderr tail:\n{r2a.stderr[-3000:]}")
    assert r2a.returncode == 0, "alice read2 did not find round2-from-bob"
