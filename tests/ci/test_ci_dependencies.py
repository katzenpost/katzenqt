import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_ci_installs_dependencies_without_developer_setup() -> None:
    for name in (
        "pytest.yml", "alembic-check.yml", "test-integration-namenlos.yml",
    ):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "make deps" not in workflow
        assert "bash tools/ci-deps.sh" in workflow
    script = (ROOT / "tools/ci-deps.sh").read_text()
    assert "apt-get update" in script
    assert script.index("apt-get update") < script.index("apt-get install")
    assert "--locked" in script
    assert "--python 3.12" in script
    assert "libgl1" in script
    assert "PySide6.QtGui" in script
    assert "systemctl" not in script
    assert "pipx" not in script
    assert "pytest" not in script


def test_install_failure_stops_before_python_setup(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls"
    for name, body in {
        "sudo": 'test "$1" = -n; shift; exec "$@"',
        "apt-get": (
            'test "$DEBIAN_FRONTEND" = noninteractive\n'
            'printf "apt-get %s\\n" "$*" >> "$CALLS"; exit 23'
        ),
        "uv": 'printf "uv %s\\n" "$*" >> "$CALLS"',
    }.items():
        path = binary / name
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n", encoding="ascii")
        path.chmod(0o755)
    run = subprocess.run(
        ["bash", str(ROOT / "tools/ci-deps.sh")], cwd=tmp_path,
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}",
                 CALLS=str(calls)),
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 23, run.stdout + run.stderr
    recorded = calls.read_text(encoding="ascii").splitlines()
    assert len(recorded) == 1
    assert recorded[0].startswith("apt-get update ")
    assert "Timeout=30" in recorded[0]
