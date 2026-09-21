import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="ascii")
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    binary = tmp_path / "bin"
    binary.mkdir()
    runner = tmp_path / "runner"
    runner.mkdir()
    _executable(
        binary / "sudo",
        'test "$1" = -n\nshift\nexec "$@"\n',
    )
    _executable(
        binary / "apt-get",
        'test "$DEBIAN_FRONTEND" = noninteractive\n'
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'case " $* " in\n'
        '    *" $FAIL_PHASE "*) exit 23 ;;\n'
        'esac\n',
    )
    for name in ("cargo", "rustc"):
        _executable(
            binary / name,
            'printf "wrong compiler path\\n" >&2\nexit 91\n',
        )
    for name in ("cargo", "rustc"):
        _executable(
            binary / f"{name}-1.89",
            f'test "$1" = --version\nprintf "{name} 1.89.0\\n"\n',
        )
    return dict(
        os.environ, PATH=f"{binary}:/usr/bin:/bin", CALLS=str(tmp_path / "calls"),
        FAIL_PHASE="never", RUNNER_TEMP=str(runner),
        GITHUB_ENV=str(tmp_path / "env"), GITHUB_PATH=str(tmp_path / "path"),
        RUSTC="not-installed",
        CARGO="not-installed", CARGO_HOME=str(tmp_path / "cargo-home"),
    )


def _run_setup(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "tools/ci-rust.sh")],
        env=env, capture_output=True, text=True, timeout=10,
    )


def test_jobs_select_rust_before_building_python_dependencies() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for block in text.split("    steps:")[1:]:
            if "ci-deps.sh" in block:
                assert "ci-rust.sh" in block
                assert block.index("ci-rust.sh") < block.index("ci-deps.sh")
    script = (ROOT / "tools/ci-rust.sh").read_text(encoding="ascii")
    assert "GITHUB_ENV" in script
    assert "GITHUB_PATH" in script


@pytest.mark.parametrize("unversioned_present", [False, True])
def test_packaged_compilers_reach_later_steps(
    tmp_path: Path, unversioned_present: bool,
) -> None:
    env = _environment(tmp_path)
    if not unversioned_present:
        for name in ("cargo", "rustc"):
            (tmp_path / "bin" / name).unlink()
        system = tmp_path / "system"
        system.mkdir()
        for name in ("bash", "dirname", "env", "ln", "mktemp"):
            source = shutil.which(name)
            assert source is not None
            (system / name).symlink_to(source)
        env["PATH"] = f"{tmp_path / 'bin'}:{system}"
    run = _run_setup(env)
    assert run.returncode == 0, run.stdout + run.stderr
    calls = (tmp_path / "calls").read_text(encoding="ascii").splitlines()
    assert len(calls) == 2
    assert "update" in calls[0]
    assert "install" in calls[1]
    assert "cargo-1.89" in calls[1] and "rustc-1.89" in calls[1]
    assert all("Timeout=30" in line for line in calls)
    published = dict(
        line.split("=", 1)
        for line in (tmp_path / "env").read_text(encoding="ascii").splitlines()
    )
    assert set(published) == {"CARGO", "RUSTC"}
    selected = (tmp_path / "path").read_text(encoding="ascii").strip()
    assert Path(selected).is_relative_to(tmp_path / "runner")
    later = subprocess.run(
        ["bash", "-c", 'cargo --version; rustc --version; "$RUSTC" --version'],
        env=dict(env, **published, PATH=f"{selected}:{env['PATH']}"),
        capture_output=True, text=True, timeout=10,
    )
    assert later.returncode == 0, later.stdout + later.stderr
    assert later.stdout.splitlines() == [
        "cargo 1.89.0", "rustc 1.89.0", "rustc 1.89.0",
    ]
    assert not Path(env["CARGO_HOME"]).exists()



def test_existing_compilers_skip_package_install(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    for name in ("cargo", "rustc"):
        _executable(
            tmp_path / "bin" / name,
            f'test "$1" = --version\nprintf "{name} 1.98.1\n"\n',
        )
    run = _run_setup(env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert not (tmp_path / "calls").exists()
    published = dict(
        line.split("=", 1)
        for line in (tmp_path / "env").read_text(encoding="ascii").splitlines()
    )
    assert published["CARGO"] == str(tmp_path / "bin/cargo")
    assert published["RUSTC"] == str(tmp_path / "bin/rustc")


@pytest.mark.parametrize("phase", ["update", "install"])
def test_package_failure_does_not_publish_compilers(
    tmp_path: Path, phase: str,
) -> None:
    env = _environment(tmp_path)
    env["FAIL_PHASE"] = phase
    run = _run_setup(env)
    assert run.returncode == 23, run.stdout + run.stderr
    assert not (tmp_path / "env").exists()
    assert not (tmp_path / "path").exists()
    calls = (tmp_path / "calls").read_text(encoding="ascii").splitlines()
    assert len(calls) == (1 if phase == "update" else 2)


def test_broken_compiler_does_not_publish_environment(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    _executable(tmp_path / "bin/rustc-1.89", "exit 29\n")
    run = _run_setup(env)
    assert run.returncode == 29, run.stdout + run.stderr
    assert not (tmp_path / "env").exists()
    assert not (tmp_path / "path").exists()


def test_missing_compiler_is_not_replaced_by_a_shim(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    (tmp_path / "bin/cargo-1.89").unlink()
    run = _run_setup(env)
    assert run.returncode != 0
    assert not (tmp_path / "env").exists()
    assert not (tmp_path / "path").exists()


def test_rust_setup_has_its_own_bound_inside_the_job_budget() -> None:
    commands = [
        line.strip().removeprefix("run: ")
        for path in (ROOT / ".github/workflows").glob("*.yml")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("run:") and "ci-rust.sh" in line
    ]
    assert len(commands) == 5
    local_bound = "timeout --kill-after=10s 300s bash tools/ci-rust.sh"
    assert commands.count(local_bound) == 2
    assert commands.count("bash tools/ci-timeout.sh " + local_bound) == 3
