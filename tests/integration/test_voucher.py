"""End-to-end Contact Voucher handshake over the docker mixnet.

Skipped unless ``KATZENQT_DOCKER_INTEGRATION=1`` (see conftest.py).

Bob (the joiner) mints a Voucher over his MessageStream and publishes it
to VoucherStream box 0; Alice (the inductor) reads it, seals a reply
carrying her read cap to box 1, and adds Bob from his salt-mutated read
cap; Bob opens the reply, moves his write cap onto the salt-mutated
sequence, and adds Alice. Both roles run as separate subprocesses, each
with its own ``KQT_STATE`` SQLite, sharing only the mixnet.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.integration._bounce_helpers import epoch_duration_s

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"
_PYTHON = os.environ.get(
    "KATZENQT_INTEGRATION_PYTHON",
    str(_VENV_PY) if _VENV_PY.exists() else sys.executable,
)

# Opt-in per-phase timing for the slow-path investigation (REPORT.md) and the
# per-hop daemon-leg table. Off by default so normal runs are unaffected.
_TIMING = os.environ.get("KQT_INTEGRATION_TIMING") == "1"

# Outer subprocess bound for the "send" verb: comfortably above
# _send_one_gcm's own wall-clock budget (KQT_SEND_BUDGET_FLOOR_S, default
# 120s; see katzenqt.headless._actions._send_one_gcm), so raising that
# floor for CI can't silently eat this margin again.
_SEND_TIMEOUT_S = float(os.environ.get("KQT_SEND_BUDGET_FLOOR_S", "120.0")) + 180.0

_VOUCHER_MARKERS = (
    " returned after ",
    " not present yet after ",
    "retrying in ",
    "still not present after ",
)


def _timed_run(what: str, role_state: Path, *cli_args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    """Run a role subprocess, printing wall-clock elapsed plus any voucher
    round-timing lines from its captured stderr when KQT_INTEGRATION_TIMING=1."""
    t0 = time.perf_counter()
    proc = _run_role(role_state, *cli_args, timeout=timeout)
    if _TIMING:
        print(f"[KQT-TIMING] {what}: {time.perf_counter() - t0:.2f}s", flush=True)
        for line in _output(proc).splitlines():
            if any(m in line for m in _VOUCHER_MARKERS):
                print(f"[KQT-VOUCHER] {line.strip()}", flush=True)
    return proc

# Connecting verbs require an explicit kpclientd connection. The docker mixnet's
# kpclientd listens on TCP 127.0.0.1:64331 (override via KATZENQT_KPCLIENTD_HOST
# / KATZENQT_KPCLIENTD_PORT, matching conftest).
_KP_ADDR = "{}:{}".format(
    os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1"),
    os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
)
_CONN_ARGS = ("--address", _KP_ADDR, "--network", "tcp")


def _read_deadline_s() -> str:
    """The read verb's poll deadline in seconds as a CLI arg string. Epoch-
    derived with the same +480s headroom as the watchdog read budget: a peer
    connection rides out at least one epoch boundary, and a send may have
    consumed the full _send_one_gcm budget before the message lands."""
    return str(int(epoch_duration_s() + 480.0))


def _read_timeout_s() -> float:
    """Outer subprocess bound for the read verbs, above their poll deadline."""
    return epoch_duration_s() + 540.0


def _role_command(role_state: Path, *cli_args: str) -> list[str]:
    # ``info`` inspects the state file only and accepts no connection flags.
    conn_args = () if cli_args and cli_args[0] == "info" else _CONN_ARGS
    return [_PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *conn_args]


def _role_env(role_state: Path) -> dict:
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    return env


def _run_role(role_state: Path, *cli_args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        _role_command(role_state, *cli_args), env=_role_env(role_state),
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )


def _spawn_role(
    role_state: Path, *cli_args: str, stdout_path: Path, stderr_path: Path,
) -> subprocess.Popen:
    """Launch a role subprocess without waiting for it to finish. Used to keep
    a joiner's ``voucher-await`` poll alive while the inductor writes box 1.
    Stdout/stderr go to files rather than pipes to avoid the 64KB
    pipe-buffer deadlock (see _bounce_helpers.spawn_role, which this
    mirrors)."""
    return subprocess.Popen(
        _role_command(role_state, *cli_args), env=_role_env(role_state),
        cwd=str(_REPO_ROOT),
        stdout=open(stdout_path, "w"),
        stderr=open(stderr_path, "w"),
        text=True,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def _assert_ok(proc: subprocess.CompletedProcess, what: str) -> None:
    assert proc.returncode == 0, (
        f"{what} failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    """Find a logged line containing token; return the text after it. The
    runner emits results through logging (stderr) with a level/name prefix,
    so match by substring rather than line start."""
    for line in _output(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _expect_info(proc: subprocess.CompletedProcess) -> dict:
    """The ``info`` verb logs one line of bare JSON on stderr; parse it."""
    for line in _output(proc).splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except ValueError:
                continue
    raise AssertionError(
        f"no JSON info line:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@pytest.mark.integration
def test_voucher_handshake_then_bidirectional(kpclientd_endpoint, tmp_path_factory):
    """Full handshake, then a message each way. The Bob -> Alice leg is the
    crux: it rides Bob's salt-mutated write cap and Alice's salt-mutated
    read cap, which must address the same boxes."""
    alice_state = tmp_path_factory.mktemp("alice") / "state"
    bob_state = tmp_path_factory.mktemp("bob") / "state"

    # Each party provisions its own MessageStream (write/read cap).
    _assert_ok(_timed_run("alice create-conv", alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_timed_run("bob create-conv", bob_state, "create-conv", "demo", "bob"), "bob create-conv")

    # Bob mints a Voucher and publishes his payload to box 0.
    mint = _timed_run("bob voucher-mint", bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    _assert_ok(mint, "bob voucher-mint")
    voucher = _expect_token(mint, "VOUCHER=")
    assert voucher, "empty voucher"

    # Alice inducts Bob with the out-of-band voucher.
    induct = _timed_run("alice voucher-induct", alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    _assert_ok(induct, "alice voucher-induct")
    assert "INDUCTED=" in _output(induct)

    # Bob polls box 1, opens the reply, and joins.
    joined = _timed_run("bob voucher-await", bob_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined, "bob voucher-await")
    assert "JOINED" in _output(joined)

    # Alice -> Bob: Bob holds Alice's read cap from the WhoReply.
    _assert_ok(_timed_run("alice send", alice_state, "send", "demo", "hello from alice", timeout=_SEND_TIMEOUT_S), "alice send")
    read_bob = _timed_run("bob read", bob_state, "read", "demo", _read_deadline_s(), "hello from alice", timeout=_read_timeout_s())
    _assert_ok(read_bob, "bob read")
    assert _expect_token(read_bob, "RECV=") == "hello from alice"

    # Bob -> Alice on the salt-mutated stream: Alice holds Bob's mutated
    # read cap from induction. This is the cross-mutation crux.
    _assert_ok(_timed_run("bob send", bob_state, "send", "demo", "hello from bob", timeout=_SEND_TIMEOUT_S), "bob send")
    read_alice = _timed_run("alice read", alice_state, "read", "demo", _read_deadline_s(), "hello from bob", timeout=_read_timeout_s())
    _assert_ok(read_alice, "alice read")
    assert _expect_token(read_alice, "RECV=") == "hello from bob"


@pytest.mark.integration
def test_voucher_await_resumes_after_crash(kpclientd_endpoint, tmp_path_factory):
    """Bob mints, then his first voucher-await is killed mid-poll (the
    PendingVoucher row survives on disk). After Alice inducts, a second
    voucher-await resumes from that row and joins, proving crash recovery."""
    alice_state = tmp_path_factory.mktemp("alice2") / "state"
    bob_state = tmp_path_factory.mktemp("bob2") / "state"

    _assert_ok(_run_role(alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_run_role(bob_state, "create-conv", "demo", "bob"), "bob create-conv")

    mint = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    _assert_ok(mint, "bob voucher-mint")
    voucher = _expect_token(mint, "VOUCHER=")

    # Kill the first await before Alice has replied: box 1 does not exist
    # yet, so the poll blocks and the subprocess is terminated on timeout.
    with pytest.raises(subprocess.TimeoutExpired):
        _run_role(bob_state, "voucher-await", "demo", timeout=25.0)

    # Now Alice replies.
    induct = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    _assert_ok(induct, "alice voucher-induct")

    # A fresh await must resume from the persisted PendingVoucher and join.
    joined = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined, "bob voucher-await (resumed)")
    assert "JOINED" in _output(joined)


@pytest.mark.integration
def test_voucher_overlapping_await(kpclientd_endpoint, tmp_path_factory):
    """The GUI interleaving: the joiner's poll of box 1 is already in flight
    (riding out BoxIDNotFound) before the inductor writes the reply, rather
    than starting after it like the other tests. A poll that precedes the
    write must still collect the reply the moment it exists.

    Every other test awaits only after the inductor has replied, so a stale
    ride-out read would silently miss the late-written box; this ordering is
    what carol's GUI hit (await started ~2min before alice inducted) and it
    never returned."""
    alice_state = tmp_path_factory.mktemp("alice_olap") / "state"
    carol_state = tmp_path_factory.mktemp("carol_olap") / "state"
    log_dir = tmp_path_factory.mktemp("carol_await_logs")
    await_out = log_dir / "await.out"
    await_err = log_dir / "await.err"

    _assert_ok(_run_role(alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_run_role(carol_state, "create-conv", "demo", "carol"), "carol create-conv")

    mint = _run_role(carol_state, "voucher-mint", "demo", "carol", timeout=300.0)
    _assert_ok(mint, "carol voucher-mint")
    voucher = _expect_token(mint, "VOUCHER=")
    assert voucher, "empty voucher"

    # Start the joiner's poll first; it rides out an unwritten box 1.
    await_proc = _spawn_role(
        carol_state, "voucher-await", "demo",
        stdout_path=await_out, stderr_path=await_err,
    )
    t_spawn = time.perf_counter()
    try:
        # Give the poll time to reach the daemon and span at least one PKI
        # epoch BEFORE the reply appears. That epoch-crossing is the core of
        # the regression this test guards: a stale ride-out read that
        # started a full epoch before the inductor wrote box 1 must still
        # collect it once the reply lands. epoch_duration_s() + margin
        # guarantees the poll observed one boundary (the next is at most an
        # epoch away).
        time.sleep(epoch_duration_s() + 20.0)
        if _TIMING:
            print(
                f"[KQT-TIMING] overlap sleep_done: {time.perf_counter() - t_spawn:.2f}s",
                flush=True,
            )
        t_induct = time.perf_counter()
        induct = _run_role(alice_state, "voucher-induct", "demo", "carol", voucher, timeout=300.0)
        _assert_ok(induct, "alice voucher-induct carol")
        if _TIMING:
            print(
                f"[KQT-TIMING] overlap induct: {time.perf_counter() - t_induct:.2f}s",
                flush=True,
            )
        await_proc.wait(timeout=300.0)
    finally:
        if await_proc.poll() is None:
            await_proc.kill()
            await_proc.wait()

    output = await_out.read_text() + await_err.read_text()
    assert await_proc.returncode == 0, (
        f"overlapping await failed (rc={await_proc.returncode}):\n{output}"
    )
    assert "JOINED" in output, output


@pytest.mark.integration
def test_voucher_3party(kpclientd_endpoint, tmp_path_factory):
    """Three-way membership: after Alice and Bob pair off, Bob inducts Carol,
    and all three end up able to read one another.

    Membership changes travel as an INTRODUCTION message on the *inductor's*
    BACAP stream, so Alice (who already reads Bob's stream) learns about Carol
    without any extra handshake: her read must surface both the ``RECV_ADD``
    announcement and Carol's subsequent message. Carol, joining later, must
    receive the whole group's pre-join history, and must not subscribe to her
    own stream (the announcement about herself is stored but not applied)."""
    alice_state = tmp_path_factory.mktemp("alice3") / "state"
    bob_state = tmp_path_factory.mktemp("bob3") / "state"
    carol_state = tmp_path_factory.mktemp("carol3") / "state"

    # Phase 0: the plain two-party handshake (mirrors
    # test_voucher_handshake_then_bidirectional).
    _assert_ok(_run_role(alice_state, "create-conv", "demo", "alice"), "alice create-conv")
    _assert_ok(_run_role(bob_state, "create-conv", "demo", "bob"), "bob create-conv")

    mint_ab = _run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    _assert_ok(mint_ab, "bob voucher-mint")
    voucher_ab = _expect_token(mint_ab, "VOUCHER=")
    assert voucher_ab, "empty voucher"

    induct_ab = _run_role(alice_state, "voucher-induct", "demo", "bob", voucher_ab, timeout=300.0)
    _assert_ok(induct_ab, "alice voucher-induct bob")
    assert "INDUCTED=" in _output(induct_ab)

    joined_bob = _run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined_bob, "bob voucher-await")
    assert "JOINED" in _output(joined_bob)

    _assert_ok(_run_role(alice_state, "send", "demo", "hello from alice", timeout=_SEND_TIMEOUT_S), "alice send")
    read_bob = _run_role(bob_state, "read", "demo", _read_deadline_s(), "hello from alice", timeout=_read_timeout_s())
    _assert_ok(read_bob, "bob read alice")
    assert _expect_token(read_bob, "RECV=") == "hello from alice"

    _assert_ok(_run_role(bob_state, "send", "demo", "hello from bob", timeout=_SEND_TIMEOUT_S), "bob send")
    read_alice_bob = _run_role(alice_state, "read", "demo", _read_deadline_s(), "hello from bob", timeout=_read_timeout_s())
    _assert_ok(read_alice_bob, "alice read bob")
    assert _expect_token(read_alice_bob, "RECV=") == "hello from bob"

    # Phase 1: Carol joins via Bob, the group's third member.
    _assert_ok(_run_role(carol_state, "create-conv", "demo", "carol"), "carol create-conv")
    mint_bc = _run_role(carol_state, "voucher-mint", "demo", "carol", timeout=300.0)
    _assert_ok(mint_bc, "carol voucher-mint")
    voucher_bc = _expect_token(mint_bc, "VOUCHER=")
    assert voucher_bc, "empty voucher"

    induct_bc = _run_role(bob_state, "voucher-induct", "demo", "carol", voucher_bc, timeout=300.0)
    _assert_ok(induct_bc, "bob voucher-induct carol")
    assert "INDUCTED=" in _output(induct_bc)

    joined_carol = _run_role(carol_state, "voucher-await", "demo", timeout=300.0)
    _assert_ok(joined_carol, "carol voucher-await")
    assert "JOINED" in _output(joined_carol)

    _assert_ok(_run_role(carol_state, "send", "demo", "hello from carol", timeout=_SEND_TIMEOUT_S), "carol send")
    read_bob_carol = _run_role(bob_state, "read", "demo", _read_deadline_s(), "hello from carol", timeout=_read_timeout_s())
    _assert_ok(read_bob_carol, "bob read carol")
    assert _expect_token(read_bob_carol, "RECV=") == "hello from carol"

    # Alice must learn about Carol from the INTRODUCTION Bob wrote to his own
    # stream, then read Carol's message without any further coordination.
    read_alice_carol = _run_role(alice_state, "read", "demo", _read_deadline_s(), "hello from carol", timeout=_read_timeout_s())
    _assert_ok(read_alice_carol, "alice read carol")
    alice_out = _output(read_alice_carol)
    assert "RECV_ADD=bob added carol" in alice_out, alice_out
    assert _expect_token(read_alice_carol, "RECV=") == "hello from carol"

    # Carol sees the group's pre-join history.
    read_carol_alice = _run_role(carol_state, "read", "demo", _read_deadline_s(), "hello from alice", timeout=_read_timeout_s())
    _assert_ok(read_carol_alice, "carol read alice")
    assert _expect_token(read_carol_alice, "RECV=") == "hello from alice"

    read_carol_bob = _run_role(carol_state, "read", "demo", _read_deadline_s(), "hello from bob", timeout=_read_timeout_s())
    _assert_ok(read_carol_bob, "carol read bob")
    assert _expect_token(read_carol_bob, "RECV=") == "hello from bob"

    # Carol receives Bob's announcement about herself but must not subscribe to
    # her own stream: exactly own + alice + bob.
    info_carol = _run_role(carol_state, "info", timeout=60.0)
    _assert_ok(info_carol, "carol info")
    info = _expect_info(info_carol)
    demo = next(
        c for c in info["conversations"] if c["name"] == "demo"
    )
    assert demo["peer_count"] == 3, f"Carol subscribed to herself? {demo}"
