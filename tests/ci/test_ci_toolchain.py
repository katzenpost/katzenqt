import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_jobs_select_rust_before_building_python_dependencies() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        for block in text.split("    steps:")[1:]:
            if "ci-deps.sh" in block:
                assert "ci-rust.sh" in block
                assert block.index("ci-rust.sh") < block.index("ci-deps.sh")
    script = (ROOT / "tools/ci-rust.sh").read_text()
    assert "toolchain=1.98.1" in script
    assert "--profile minimal" in script
    assert "rustup default" not in script
    assert "GITHUB_ENV" in script
    assert "GITHUB_PATH" in script


def test_toolchain_selection_reaches_later_steps(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    rustup = binary / "rustup"
    rustup.write_text(
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'if [ "$1" = which ]; then printf "/selected/bin/cargo\\n"; fi\n',
        encoding="ascii",
    )
    rustup.chmod(0o755)
    calls = tmp_path / "calls"
    env_file, path_file = tmp_path / "env", tmp_path / "path"
    run = subprocess.run(
        ["bash", str(ROOT / "tools/ci-rust.sh")], cwd=tmp_path,
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}",
                 CARGO_HOME=str(tmp_path / "cargo"), CALLS=str(calls),
                 GITHUB_ENV=str(env_file), GITHUB_PATH=str(path_file)),
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert env_file.read_text() == "RUSTUP_TOOLCHAIN=1.98.1\n"
    assert path_file.read_text() == "/selected/bin\n"
    assert calls.read_text().splitlines() == [
        "toolchain install 1.98.1 --profile minimal --no-self-update",
        "run 1.98.1 rustc --version", "run 1.98.1 cargo --version",
        "which --toolchain 1.98.1 cargo",
    ]


def test_package_rustup_does_not_try_to_update_itself(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "usr-bin"
    binary.mkdir()
    rustup = binary / "rustup"
    rustup.write_text(
        'set -eu\n'
        'if [ "$1" = toolchain ]; then\n'
        '    case " $* " in\n'
        '        *" --no-self-update "*) ;;\n'
        '        *) printf "rustup is not installed at %s\\n" '
        '"$CARGO_HOME" >&2; exit 1 ;;\n'
        '    esac\n'
        'elif [ "$1" = which ]; then\n'
        '    printf "/selected/bin/cargo\\n"\n'
        'fi\n',
        encoding="ascii",
    )
    rustup.chmod(0o755)
    cargo_home = tmp_path / "cargo"
    cargo_home.mkdir()
    env_file, path_file = tmp_path / "env", tmp_path / "path"
    result = subprocess.run(
        ["bash", str(ROOT / "tools/ci-rust.sh")],
        env=dict(
            os.environ, PATH=f"{binary}:{os.environ['PATH']}",
            CARGO_HOME=str(cargo_home),
            GITHUB_ENV=str(env_file), GITHUB_PATH=str(path_file),
        ),
        capture_output=True, text=True, timeout=10,
    )
    assert not (cargo_home / "bin/rustup").exists()
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.read_text() == "RUSTUP_TOOLCHAIN=1.98.1\n"
    assert path_file.read_text() == "/selected/bin\n"


def test_rust_setup_has_its_own_bound_inside_the_job_budget() -> None:
    commands = [
        line.strip().removeprefix("run: ")
        for path in (ROOT / ".github/workflows").glob("*.yml")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("run:") and "ci-rust.sh" in line
    ]
    assert len(commands) == 5
    local_bound = (
        "timeout --kill-after=10s 300s bash tools/ci-rust.sh"
    )
    assert commands.count(local_bound) == 2
    assert commands.count(
        "bash tools/ci-timeout.sh " + local_bound
    ) == 3
