from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
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
        ("curl", shutil.which("curl")),
        ("id", shutil.which("id")),
        ("mkdir", shutil.which("mkdir")),
        ("python3", sys.executable),
    ):
        assert source is not None
        (binary / name).symlink_to(source)
    for name in ("git", "podman", "systemctl", "timeout"):
        path = binary / name
        path.write_text(
            'printf "%s\\n" "$0 $*" >> "$CALLS/forbidden"\nexit 91\n',
            encoding="ascii",
        )
        path.chmod(0o755)
    act = binary / "act"
    act.write_text(
        'printf "%s\\n" "$PWD" > "$CALLS/cwd"\n'
        'printf "%s\\n" "$DOCKER_HOST" > "$CALLS/endpoint"\n'
        'printf "%s\\n" "$@" > "$CALLS/args"\n'
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
    arguments = (calls / "args").read_text().splitlines()
    assert arguments == [
        "--concurrent-jobs", "1", "--network", "host",
        "--container-daemon-socket", f"unix://{run.socket}",
        "--container-options",
        f'--volume "{run.directory}/.ci-local:{run.directory}/.ci-local"',
        "--artifact-server-path", str(run.directory / ".ci-local/artifacts"),
        "--artifact-server-addr", "127.0.0.1",
    ]
    assert (run.directory / ".ci-local").is_dir()
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


@pytest.mark.parametrize("name", ["act", "curl"])
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
