from pathlib import Path
import subprocess
import tempfile


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
        try:
            result = subprocess.run(
                command, env=env, cwd=cwd, stdout=out, stderr=err,
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
    return subprocess.CompletedProcess(
        result.args, result.returncode,
        Path(out.name).read_text(encoding="utf-8", errors="replace"),
        Path(err.name).read_text(encoding="utf-8", errors="replace"),
    )
