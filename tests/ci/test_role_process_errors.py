import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.integration._process import run_logged

ROOT = Path(__file__).resolve().parents[2]


def _fake_cli(tmp_path: Path, code: str) -> dict[str, str]:
    package = tmp_path / "katzenqt"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="ascii")
    (package / "integration_runner.py").write_text(
        "import logging\nimport sys\n"
        f"def main(args=None):\n{code}\n"
        "if __name__ == '__main__':\n    sys.exit(main())\n",
        encoding="ascii",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(ROOT)])
    return env


def test_logged_python_exception_cannot_pass_a_role(tmp_path: Path) -> None:
    env = _fake_cli(tmp_path,
        "    try:\n"
        "        raise RuntimeError('lost background task')\n"
        "    except RuntimeError:\n"
        "        logging.exception('background failure')\n"
        "    return 0"
    )
    with pytest.raises(AssertionError):
        run_logged(
            tmp_path / "state",
            [sys.executable, "-m", "katzenqt.integration_runner", "read"],
            env=env, cwd=str(tmp_path), timeout=10,
        )
    assert "RuntimeError" in next(tmp_path.glob("*.err")).read_text()


def test_unexpected_python_timeout_is_not_a_delivery_deadline(
    tmp_path: Path,
) -> None:
    env = _fake_cli(tmp_path, "    raise TimeoutError('unexpected timeout')")
    with pytest.raises(AssertionError):
        run_logged(
            tmp_path / "state",
            [sys.executable, "-m", "katzenqt.integration_runner", "read"],
            env=env, cwd=str(tmp_path), timeout=10,
        )


def test_parent_process_timeout_remains_a_process_error(
    tmp_path: Path,
) -> None:
    env = _fake_cli(
        tmp_path, "    import time\n    time.sleep(30)\n    return 0",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged(
            tmp_path / "state",
            [sys.executable, "-m", "katzenqt.integration_runner", "read"],
            env=env, cwd=str(tmp_path), timeout=0.2,
        )
