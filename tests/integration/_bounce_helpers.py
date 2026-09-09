"""Shared subprocess/voucher/podman helpers for the integration tests.

The subprocess and voucher helpers were previously duplicated verbatim
between test_restart.py and test_client_reconnect.py, where the two copies
had already started to drift (test_restart.py's _expect_token routed
through a _combined() helper; test_client_reconnect.py's inlined
`proc.stdout + proc.stderr` directly). One copy here so a fix to how these
subprocesses are launched or their output parsed can't be applied to one
file and missed in the other.

The podman helpers (kpclientd_reachable, find_kpclientd_container,
find_same_network_container, podman, wait_reachable) serve the watchdog
tests that live-verify container restarts and mixnet blips
(test_watchdog_reconnect.py, test_watchdog_mixnet_reconnect.py).

epoch_duration_s() is the mixnet's actual epoch length, for the handful of
tests whose waits must span (or deliberately stay under) a PKI epoch
boundary -- so those waits scale with whatever network they're pointed at
instead of assuming the docker mixnet's 2m default.
"""
from __future__ import annotations

import functools
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

# Opt-in per-phase timing for the integration-suite slow-path investigation
# (REPORT.md). Off by default so normal runs are unaffected.
_TIMING = os.environ.get("KQT_INTEGRATION_TIMING") == "1"


class PhaseStopwatch:
    """Measure phases of a slow integration scenario.

    With KQT_INTEGRATION_TIMING=1 each :meth:`mark` prints ``[KQT-TIMING]
    <what> <phase>: <elapsed>s`` (wall time since the stopwatch began), so a
    solo run exposes exactly where the wall clock went. Cheap no-op
    otherwise.
    """

    def __init__(self, what: str):
        self._what = what
        self._t0 = time.perf_counter()
        self._last = self._t0

    def mark(self, phase: str) -> None:
        now = time.perf_counter()
        if _TIMING:
            print(
                f"[KQT-TIMING] {self._what} {phase}: {now - self._t0:.2f}s "
                f"(d={now - self._last:.2f}s)",
                flush=True,
            )
        self._last = now


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VENV_PY = REPO_ROOT / ".venv" / "bin" / "python3"
PYTHON = os.environ.get(
    "KATZENQT_INTEGRATION_PYTHON",
    str(_VENV_PY) if _VENV_PY.exists() else sys.executable,
)

_GO_DURATION_TERM_RE = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s)")
_ENV_LINE_RE = re.compile(r"KATZENPOST_EPOCH_DURATION=(\S+)")


def _parse_go_duration(text: str) -> float:
    """Parse a Go time.ParseDuration-style string ("2m", "1h30m", "45s")
    into seconds. Only h/m/s/ms are needed here: genconfig's --epochDuration
    flag is passed straight through as this string."""
    total = 0.0
    matched = False
    for amount, unit in _GO_DURATION_TERM_RE.findall(text):
        matched = True
        scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}[unit]
        total += float(amount) * scale
    if not matched:
        raise ValueError(f"not a Go duration string: {text!r}")
    return total


@functools.lru_cache(maxsize=1)
def epoch_duration_s() -> float:
    """The mixnet's actual epoch length in seconds.

    KQT_EPOCH_DURATION_S overrides everything else, for targets with no
    docker-compose.yml to read (e.g. namenlos). Otherwise this comes from
    the generated docker-compose.yml: genconfig's GenDockerCompose writes
    KATZENPOST_EPOCH_DURATION into every service's environment block from
    the same --epochDuration value the mixnet's own processes were started
    with, so this reads the value actually governing the running network
    rather than a second guess at it.
    """
    override = os.environ.get("KQT_EPOCH_DURATION_S")
    if override:
        return float(override)
    compose_path = Path(os.environ.get(
        "KATZENPOST_DOCKER_COMPOSE",
        str(REPO_ROOT / "katzenpost" / "docker" / "voting_mixnet" / "docker-compose.yml"),
    ))
    try:
        text = compose_path.read_text()
    except OSError as e:
        raise RuntimeError(
            f"can't determine epoch_duration: {compose_path} not readable "
            f"({e}); set KQT_EPOCH_DURATION_S to override"
        ) from e
    match = _ENV_LINE_RE.search(text)
    if not match:
        raise RuntimeError(
            f"can't determine epoch_duration: no KATZENPOST_EPOCH_DURATION "
            f"in {compose_path}; set KQT_EPOCH_DURATION_S to override"
        )
    return _parse_go_duration(match.group(1))


# Connecting verbs require an explicit kpclientd connection. The docker mixnet's
# kpclientd listens on TCP 127.0.0.1:64331 (override via KATZENQT_KPCLIENTD_HOST
# / KATZENQT_KPCLIENTD_PORT, matching conftest).
KP_ADDR = "{}:{}".format(
    os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1"),
    os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
)
CONN_ARGS = ("--address", KP_ADDR, "--network", "tcp")


def run_role(role_state: Path, *cli_args: str, timeout: float = 300.0):
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *CONN_ARGS]
    return subprocess.run(
        cmd, env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def spawn_role(role_state: Path, *cli_args: str, stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    """Popen variant for long-running chat-session subprocesses that we
    want running in parallel. We redirect stdout/stderr to files instead
    of pipes to avoid the classic 64 KB pipe-buffer deadlock: when one
    subprocess fills its stdout pipe, it blocks on write, and if the
    parent is `communicate`-ing a different subprocess, the blocked one
    can starve long enough for its background read loop to stall.
    """
    env = os.environ.copy()
    env["KQT_STATE"] = str(role_state)
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [PYTHON, "-m", "katzenqt.integration_runner", *cli_args, *CONN_ARGS]
    return subprocess.Popen(
        cmd, env=env, cwd=str(REPO_ROOT),
        stdout=open(stdout_path, "w"),
        stderr=open(stderr_path, "w"),
        text=True,
    )


def combined(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def expect_token(proc: subprocess.CompletedProcess, token: str) -> str:
    """Find a logged line containing token; return the text after it. Results
    go through logging (stderr) with a level/name prefix, so match by
    substring."""
    for line in combined(proc).splitlines():
        idx = line.find(token)
        if idx != -1:
            return line[idx + len(token):].strip()
    raise AssertionError(
        f"no line containing {token!r}:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def bootstrap_voucher(alice_state: Path, bob_state: Path) -> None:
    """Establish mutual contact via the Contact Voucher handshake. Bob mints a
    voucher over his stream, Alice inducts him (gaining his salt-mutated read
    cap) and replies with her read cap, and Bob joins (gaining hers). Both can
    then read each other, the bidirectional state the restart tests exercise."""
    for state, name in ((alice_state, "alice"), (bob_state, "bob")):
        create = run_role(state, "create-conv", "demo", name, timeout=180.0)
        assert create.returncode == 0, create.stdout + create.stderr
    mint = run_role(bob_state, "voucher-mint", "demo", "bob", timeout=300.0)
    assert mint.returncode == 0, mint.stdout + mint.stderr
    voucher = expect_token(mint, "VOUCHER=")
    induct = run_role(alice_state, "voucher-induct", "demo", "bob", voucher, timeout=300.0)
    assert induct.returncode == 0, induct.stdout + induct.stderr
    joined = run_role(bob_state, "voucher-await", "demo", timeout=300.0)
    assert joined.returncode == 0, joined.stdout + joined.stderr


def kpclientd_reachable(timeout: float = 1.0) -> bool:
    host = os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1")
    port = int(os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_kpclientd_container() -> str:
    """The kpclientd container actually publishing KATZENQT_KPCLIENTD_PORT.

    Multiple mixnet networks (this host's other sessions/checkouts) can be
    running at once, each with its own *-kpclientd-1 container on a
    different published port; matching on name suffix alone picked
    whichever one podman listed first, which could be a container this
    test has no business touching. Correlate on the published port instead.
    """
    override = os.environ.get("KATZENQT_KPCLIENTD_CONTAINER")
    if override:
        return override
    port = os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331")
    proc = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman ps failed: {proc.stderr}")
    candidates = []
    for line in proc.stdout.splitlines():
        name, _, ports = line.partition("\t")
        if not name.endswith("-kpclientd-1"):
            continue
        candidates.append(name)
        if f":{port}->" in ports:
            return name
    if len(candidates) == 1:
        return candidates[0]  # only one on the host: unambiguous even if the port string didn't match
    raise RuntimeError(
        f"could not find a kpclientd container publishing port {port} "
        f"(candidates: {candidates or 'none'}); set KATZENQT_KPCLIENTD_CONTAINER "
        "to disambiguate"
    )


def find_same_network_container(kpclientd_container: str, role: str) -> str:
    """A sibling container (e.g. "gateway1") on the SAME compose network as
    an already-identified kpclientd container.

    Deriving the network prefix from a container we've already confirmed is
    ours (rather than matching role names against the full `podman ps`
    output again) means this can never resolve to a different session's
    network, however many are running on the host.
    """
    if not kpclientd_container.endswith("-kpclientd-1"):
        raise ValueError(f"not a kpclientd container name: {kpclientd_container!r}")
    prefix = kpclientd_container[: -len("-kpclientd-1")]
    name = f"{prefix}-{role}-1"
    proc = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman ps failed: {proc.stderr}")
    if name not in proc.stdout.split():
        raise RuntimeError(f"expected sibling container {name!r} not found running")
    return name


def podman(args) -> None:
    proc = subprocess.run(
        ["podman", *args], capture_output=True, text=True, check=False,
        timeout=120.0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args)} failed ({proc.returncode}): {proc.stderr}")


def wait_reachable(deadline_s: float) -> None:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if kpclientd_reachable():
            return
        time.sleep(1.0)
    raise AssertionError(f"kpclientd did not become reachable within {deadline_s:.0f}s")
