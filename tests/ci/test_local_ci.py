from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import shutil
import socket
from socketserver import UnixStreamServer
import subprocess
import sys
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make") or "make"


@contextmanager
def _api(
    path: Path, *, status: int = 200, payload: bytes = b"OK",
) -> Iterator[list[str]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with UnixStreamServer(str(path), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield requests
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@dataclass(frozen=True)
class _Run:
    directory: Path
    runtime: Path
    env: dict[str, str]

    @property
    def socket(self) -> Path:
        return self.runtime / "podman/podman.sock"

    def make(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [MAKE, "-f", str(ROOT / "Makefile"), "ci-local", *args],
            cwd=self.directory, env=self.env, text=True,
            capture_output=True, timeout=15,
        )


@pytest.fixture
def run(tmp_path: Path) -> Iterator[_Run]:
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, source in (
        ("awk", shutil.which("awk")),
        ("curl", shutil.which("curl")),
        ("id", shutil.which("id")),
        ("mkdir", shutil.which("mkdir")),
        ("mktemp", shutil.which("mktemp")),
        ("python3", sys.executable),
        ("rm", shutil.which("rm")),
        ("sort", shutil.which("sort")),
        ("touch", shutil.which("touch")),
        ("xargs", shutil.which("xargs")),
    ):
        assert source is not None
        (binary / name).symlink_to(source)
    for name in ("git", "systemctl", "timeout"):
        path = binary / name
        path.write_text(
            'printf "%s\\n" "$0 $*" >> "$CALLS/forbidden"\nexit 91\n',
            encoding="ascii",
        )
        path.chmod(0o755)
    podman = binary / "podman"
    podman.write_text(
        'printf "%s\n" "$*" >> "$CALLS/podman"\n'
        'ran="$CALLS/act-ran"\n'
        'if [[ "$1 $2 $3" == "ps -a --filter" ]]; then\n'
        '  if [[ "${STALE_LOCAL_CI:-}" == 1 ]]; then\n'
        '    printf "stale-local\nother-container\n"\n'
        '  fi\n'
        '  exit 0\n'
        'fi\n'
        'if [[ "$1" == inspect ]]; then\n'
        '  case "$4" in\n'
        '    stale-local) printf "%s/.ci-local/epoch-integration/katzenpost/docker/mixnet-alpine\n" "$PWD" ;;\n'
        '    other-container) printf "/tmp/other-project\n" ;;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'case "$1 $2" in\n'
        '  "image exists") exit 0 ;;\n'
        '  "ps -a") [[ -e "$ran" ]] && printf "new-container\nnew-buildkit\n" ;;\n'
        '  "volume ls") [[ -e "$ran" ]] && printf "act-test-volume\nbuildkit-volume\n" ;;\n'
        '  "images --filter") [[ -e "$ran" ]] && printf "new-dangling-image\n" ;;\n'
        'esac\n'
        'exit 0\n',
        encoding="ascii",
    )
    podman.chmod(0o755)
    act = binary / "act"
    act.write_text(
        'printf "%s\\n" "$PWD" > "$CALLS/cwd"\n'
        'printf "%s\\n" "$DOCKER_HOST" > "$CALLS/endpoint"\n'
        'printf "%s\\n" "$TMPDIR" > "$CALLS/tmpdir"\n'
        'printf "%s\\n" "$@" > "$CALLS/args"\n'
        'touch "$CALLS/act-ran"\n'
        'exit "${ACT_STATUS:-0}"\n',
        encoding="ascii",
    )
    act.chmod(0o755)
    calls = tmp_path / "calls"
    calls.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".env").write_text("UNCHANGED=yes\n", encoding="ascii")
    with tempfile.TemporaryDirectory(prefix="kqt-socket-") as value:
        runtime = Path(value)
        (runtime / "podman").mkdir()
        env = {
            "PATH": str(binary), "HOME": str(home), "CALLS": str(calls),
            "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_RUNTIME_DIR": str(runtime),
            "http_proxy": "http://127.0.0.1:1",
        }
        yield _Run(tmp_path, runtime, env)
    assert not (calls / "forbidden").exists()
    assert list(home.iterdir()) == []
    assert (tmp_path / ".env").read_text() == "UNCHANGED=yes\n"


def test_make_runs_act_in_place_after_pinging_socket(run: _Run) -> None:
    with _api(run.socket) as requests:
        result = run.make()
    assert result.returncode == 0, result.stdout + result.stderr
    assert requests == ["/_ping"]
    calls = run.directory / "calls"
    assert (calls / "cwd").read_text().strip() == str(run.directory)
    assert (calls / "endpoint").read_text().strip() == f"unix://{run.socket}"
    assert (calls / "tmpdir").read_text().strip() == str(
        run.directory / ".ci-local/tmp"
    )
    arguments = (calls / "args").read_text().splitlines()
    assert arguments == [
        "-P", "ubuntu-24.04=localhost/katzenqt-act:latest",
        "--rm", "--concurrent-jobs", "1", "--network", "host",
        "-P", "ubuntu-latest=localhost/katzenqt-act:latest",
        "--container-daemon-socket", f"unix://{run.socket}",
        "--container-options",
        f'--volume "{run.directory}/.ci-local:{run.directory}/.ci-local"',
        "--env", f"UV_CACHE_DIR={run.directory}/.ci-local/uv-cache",
        "--env", "UV_LINK_MODE=copy",
        "--env", f"GOMODCACHE={run.directory}/.ci-local/go-mod",
        "--env", f"GOCACHE={run.directory}/.ci-local/go-build",
        "--env", f"CARGO_HOME={run.directory}/.ci-local/cargo-home",
        "--artifact-server-path", str(run.directory / ".ci-local/artifacts"),
        "--artifact-server-addr", "127.0.0.1",
    ]
    for name in ("uv-cache", "go-mod", "go-build", "cargo-home"):
        assert (run.directory / ".ci-local" / name).is_dir()
    assert not (run.directory / ".ci-local/tmp").exists()
    assert "--bind" not in arguments


def test_act_arguments_are_forwarded_after_local_options(run: _Run) -> None:
    with _api(run.socket):
        result = run.make("ACT_ARGS=--list --job 'test name'")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (run.directory / "calls/args").read_text().splitlines()[-3:] == [
        "--list", "--job", "test name",
    ]


def test_act_failure_fails_make(run: _Run) -> None:
    run.env["ACT_STATUS"] = "7"
    with _api(run.socket):
        result = run.make()
    assert result.returncode != 0
    assert "Error 7" in result.stderr
    assert (run.directory / "calls/cwd").exists()
    assert not (run.directory / ".ci-local/ci-local.lock").exists()


def test_cleanup_removes_resources_created_by_act(run: _Run) -> None:
    run.env["ACT_STATUS"] = "7"
    with _api(run.socket):
        result = run.make()
    assert result.returncode != 0
    calls = (run.directory / "calls/podman").read_text().splitlines()
    assert "rm -f -v new-container new-buildkit" in calls
    assert "volume rm -f act-test-volume buildkit-volume" in calls
    assert "image rm -f new-dangling-image" in calls
    assert not any("new-mixnet-image" in call for call in calls)



def test_active_local_ci_lock_stops_before_act(run: _Run) -> None:
    lock = run.directory / ".ci-local/ci-local.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
    with _api(run.socket):
        result = run.make()
    assert result.returncode != 0
    assert "ci-local is already running" in result.stderr
    assert not (run.directory / "calls/cwd").exists()



def test_stale_local_ci_lock_is_recovered(run: _Run) -> None:
    lock = run.directory / ".ci-local/ci-local.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999999\n", encoding="ascii")
    with _api(run.socket):
        result = run.make()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not lock.exists()

def test_stale_local_ci_state_is_removed_before_act(run: _Run) -> None:
    run.env["STALE_LOCAL_CI"] = "1"
    with _api(run.socket):
        result = run.make()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (run.directory / "calls/podman").read_text().splitlines()
    assert "rm -f -v stale-local" in calls
    assert not any(
        call.startswith("rm -f -v") and "other-container" in call
        for call in calls
    )


def test_busy_mixnet_port_stops_before_act(run: _Run) -> None:
    python = run.directory / "bin/python3"
    python.unlink()
    python.write_text("exit 0\n", encoding="ascii")
    python.chmod(0o755)
    with _api(run.socket):
        result = run.make()
    assert result.returncode != 0
    assert "port 64331 is already in use" in result.stderr
    assert not (run.directory / "calls/cwd").exists()

def test_an_explicit_local_endpoint_is_checked_and_used(run: _Run) -> None:
    endpoint = run.runtime / "selected.sock"
    with _api(endpoint) as requests:
        result = run.make(f"DOCKER_HOST=unix://{endpoint}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert requests == ["/_ping"]
    assert (run.directory / "calls/endpoint").read_text().strip() == (
        f"unix://{endpoint}"
    )


@pytest.mark.parametrize("endpoint", [
    "tcp://example.invalid:2375", "ssh://example.invalid", "unix://relative",
])
def test_nonlocal_endpoints_are_rejected(run: _Run, endpoint: str) -> None:
    result = run.make(f"DOCKER_HOST={endpoint}")
    assert result.returncode != 0
    assert "local Unix socket" in result.stderr
    assert not (run.directory / "calls/cwd").exists()


@pytest.mark.parametrize("kind", ["missing", "file", "stopped"])
def test_unavailable_socket_stops_before_act(run: _Run, kind: str) -> None:
    if kind == "file":
        run.socket.write_text("not a socket", encoding="ascii")
    elif kind == "stopped":
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(run.socket))
    result = run.make()
    assert result.returncode != 0
    assert str(run.socket) in result.stderr
    assert not (run.directory / "calls/cwd").exists()


@pytest.mark.parametrize("status,payload", [(503, b"OK"), (200, b"bad")])
def test_bad_api_response_stops_before_act(
    run: _Run, status: int, payload: bytes,
) -> None:
    with _api(run.socket, status=status, payload=payload):
        result = run.make()
    assert result.returncode != 0
    assert "Podman API" in result.stderr
    assert not (run.directory / "calls/cwd").exists()


@pytest.mark.parametrize("name", ["act", "curl", "podman", "python3"])
def test_missing_program_has_an_actionable_error(run: _Run, name: str) -> None:
    (run.directory / "bin" / name).unlink()
    result = run.make()
    assert result.returncode != 0
    assert f"{name} is required" in result.stderr
    assert not (run.directory / "calls/cwd").exists()


def test_runtime_directory_falls_back_to_the_current_uid(run: _Run) -> None:
    del run.env["XDG_RUNTIME_DIR"]
    result = run.make("-n")
    assert result.returncode == 0, result.stderr
    assert "/run/user/$(id -u)" in result.stdout


def test_workflows_keep_standard_checkout_and_podman_setup() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="ascii")
        for block in text.split("      - name: ")[1:]:
            if "uses: actions/checkout@" in block:
                assert "env.ACT" not in block
        assert "ci-engine.sh" not in text
    workflow = ROOT / ".github/workflows/test-integration-namenlos.yml"
    assert workflow.read_text(encoding="ascii").count(
        "      - name: Configure podman socket\n"
        "        run: systemctl --user start podman.socket || true\n"
    ) == 2


def test_make_keeps_spaces_in_the_shared_mount_path(run: _Run) -> None:
    directory = run.directory / "checkout with spaces"
    directory.mkdir()
    local = _Run(directory, run.runtime, run.env)
    with _api(run.socket):
        result = local.make()
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = (run.directory / "calls/args").read_text().splitlines()
    volume = arguments[arguments.index("--container-options") + 1]
    assert volume == f'--volume "{directory}/.ci-local:{directory}/.ci-local"'
