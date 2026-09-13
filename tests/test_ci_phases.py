import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("parallel_status, serial_status", [(0, 0), (1, 0), (0, 1)])
def test_integration_phases_preserve_failures(
    tmp_path: Path, parallel_status: int, serial_status: int,
) -> None:
    binary = tmp_path / "venv" / "bin" / "pytest"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  *"not serial_docker"*) exit "$PARALLEL_STATUS" ;;\n'
        '  *) exit "$SERIAL_STATUS" ;;\n'
        'esac\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    calls = tmp_path / "calls"
    env = dict(
        os.environ, CALLS=str(calls),
        PARALLEL_STATUS=str(parallel_status), SERIAL_STATUS=str(serial_status),
    )
    result = subprocess.run(
        [
            "make", "-f", str(ROOT / "Makefile"), "-o", "setup",
            "docker-integration", f"VENV={binary.parent.parent}",
            "THIN_CLIENT_DIR=", "KQT_INTEGRATION_PARALLEL=3",
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    phases = calls.read_text(encoding="utf-8").splitlines()
    assert len(phases) == 2
    assert "-n 3 --dist loadscope -m not serial_docker" in phases[0]
    assert "-m serial_docker" in phases[1]
    assert "-n " not in phases[1]
    assert (result.returncode == 0) == (parallel_status == serial_status == 0)


def test_voucher_role_does_not_block_on_large_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import test_voucher

    command = [
        sys.executable, "-c",
        "import sys; sys.stderr.write('x' * 1048576); print('finished')",
    ]
    monkeypatch.setattr(test_voucher, "_role_command", lambda *args: command)
    out = tmp_path / "role.out"
    err = tmp_path / "role.err"
    process = test_voucher._spawn_role(
        tmp_path / "state", stdout_path=out, stderr_path=err,
    )
    try:
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert out.read_text(encoding="utf-8").strip() == "finished"
    assert err.stat().st_size == 1048576


@pytest.mark.parametrize("status", [0, 7])
def test_role_output_survives_completion(tmp_path: Path, status: int) -> None:
    from tests.integration._process import run_logged

    result = run_logged(
        tmp_path / "state",
        [sys.executable, "-c", f"import sys; print('output'); sys.exit({status})"],
        env=dict(os.environ), cwd=str(tmp_path), timeout=5,
    )
    assert result.returncode == status
    assert result.stdout == "output\n"
    assert next(tmp_path.glob("*.out")).read_text() == result.stdout


def test_role_output_survives_timeout(tmp_path: Path) -> None:
    from tests.integration._process import run_logged

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_logged(
            tmp_path / "state",
            [sys.executable, "-c", (
                "import sys, time; print('output', flush=True); "
                "print('waiting for reply', file=sys.stderr, flush=True); "
                "time.sleep(60)"
            )],
            env=dict(os.environ), cwd=str(tmp_path), timeout=1,
        )
    assert caught.value.output == b"output\n"
    assert caught.value.stderr == b"waiting for reply\n"
    assert "waiting for reply" in caught.value.__notes__[0]
    assert next(tmp_path.glob("*.err")).read_bytes() == caught.value.stderr
