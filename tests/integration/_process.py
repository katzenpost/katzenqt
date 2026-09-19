from pathlib import Path
import subprocess
import tempfile

from tests.integration._outcomes import check_roles, result_path


def run_logged(
    role_state: Path,
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Retain role output even when its subprocess times out."""
    with tempfile.NamedTemporaryFile(
        prefix=f"{role_state.name}-", suffix=".out", dir=role_state.parent,
        delete=False,
    ) as out, tempfile.NamedTemporaryFile(
        prefix=f"{role_state.name}-", suffix=".err", dir=role_state.parent,
        delete=False,
    ) as err:
        actual, observed = prepare_role(command, Path(err.name))
        try:
            result = subprocess.run(
                actual, env=env, cwd=cwd, stdout=out, stderr=err,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            exc.output = Path(out.name).read_bytes()
            exc.stderr = Path(err.name).read_bytes()
            exc.add_note(
                f"Role output: {out.name}, {err.name}\n"
                f"stderr tail:\n{exc.stderr[-8192:].decode('utf-8', errors='replace')}"
            )
            raise
    if observed:
        check_roles([(result.returncode, Path(err.name))])
    return subprocess.CompletedProcess(
        result.args, result.returncode,
        Path(out.name).read_text(encoding="utf-8", errors="replace"),
        Path(err.name).read_text(encoding="utf-8", errors="replace"),
    )


def prepare_role(command: list[str], stderr: Path) -> tuple[list[str], bool]:
    if command[1:3] != ["-m", "katzenqt.integration_runner"]:
        return command, False
    report = result_path(stderr)
    report.unlink(missing_ok=True)
    return [
        command[0], "-m", "tests.integration._role", str(report),
        *command[3:],
    ], True


def spawn_logged(
    command: list[str], *, env: dict[str, str], cwd: str,
    stdout_path: Path, stderr_path: Path,
) -> subprocess.Popen[str]:
    actual, _ = prepare_role(command, stderr_path)
    with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open(
        "w", encoding="utf-8",
    ) as err:
        return subprocess.Popen(
            actual, env=env, cwd=cwd, stdout=out, stderr=err, text=True,
        )
