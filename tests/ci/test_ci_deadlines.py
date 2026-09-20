import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/ci-timeout.sh"


@pytest.mark.parametrize("value, code", [("", 2), ("bad", 2), ("1", 124)])
def test_invalid_or_exhausted_budget_does_not_start_a_process(
    tmp_path: Path, value: str, code: int,
) -> None:
    marker = tmp_path / "ran"
    result = subprocess.run(
        ["bash", str(SCRIPT), sys.executable, "-c",
         f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        env=dict(os.environ, KQT_CI_DEADLINE=value),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == code
    assert not marker.exists()


def test_command_exit_status_survives_the_budget_guard() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), sys.executable, "-c", "raise SystemExit(7)"],
        env=dict(os.environ, KQT_CI_DEADLINE=str(int(time.time()) + 30)),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 7


def test_a_later_phase_gets_only_the_remaining_budget(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, content in (
        ("date", 'printf "%s\\n" "$NOW"\n'),
        ("timeout", 'printf "%s\\n" "$@"\n'),
    ):
        path = binary / name
        path.write_text(content, encoding="ascii")
        path.chmod(0o755)
    for now, remaining in ((20, 80), (90, 10)):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "command", "argument"],
            env=dict(os.environ, PATH=str(binary), NOW=str(now),
                     KQT_CI_DEADLINE="100"),
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert result.stdout.splitlines() == [
            "--kill-after=10s", f"{remaining}s", "command", "argument",
        ]
