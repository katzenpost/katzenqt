import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from tests.integration import _bounce_helpers as helpers

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("local", [False, True])
def test_dependency_paths_keep_local_mixnet_files_on_the_shared_mount(
    tmp_path: Path, local: bool,
) -> None:
    env_file = tmp_path / "env"
    environment = dict(
        os.environ, ACT="true" if local else "",
        GITHUB_WORKSPACE=str(tmp_path / "checkout"),
        GITHUB_JOB="epoch-integration", GITHUB_ENV=str(env_file),
    )
    command = (
        'test() { if [[ "$1" == -S ]]; then return 0; '
        'else builtin test "$@"; fi; }; '
        'mountpoint() { [[ "$2" == "$GITHUB_WORKSPACE/.ci-local" ]]; }; '
        'source "$1"'
    )
    run = subprocess.run(
        ["bash", "-c", command, "bash", str(ROOT / "tools/ci-paths.sh")],
        env=environment, capture_output=True, text=True, timeout=5,
    )
    assert run.returncode == 0, run.stderr
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    relative = (
        ".ci-local/epoch-integration/katzenpost"
        if local else "katzenqt/katzenpost"
    )
    assert values["KATZENPOST_CI_PATH"] == relative
    assert values["KATZENPOST_DIR"] == str(tmp_path / "checkout" / relative)
    assert values["KATZENPOST_DOCKER_COMPOSE"] == (
        f"{values['KATZENPOST_DIR']}/docker/mixnet-alpine/docker-compose.yml"
    )
    if local:
        assert values["KATZENQT_CONTAINER_ENGINE"] == "docker"
        assert values["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        assert values["CONTAINER_HOST"] == values["DOCKER_HOST"]
    else:
        assert "KATZENQT_CONTAINER_ENGINE" not in values
        assert "DOCKER_HOST" not in values


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_bounce_helpers_use_the_selected_engine(
    monkeypatch: pytest.MonkeyPatch, engine: str,
) -> None:
    monkeypatch.setenv("KATZENQT_CONTAINER_ENGINE", engine)
    monkeypatch.delenv("KATZENQT_KPCLIENTD_CONTAINER", raising=False)
    monkeypatch.setenv("KATZENQT_KPCLIENTD_PORT", "64331")
    run = Mock(return_value=subprocess.CompletedProcess(
        [], 0, "mix-kpclientd-1\t127.0.0.1:64331->64331/tcp\n", "",
    ))
    monkeypatch.setattr(helpers.subprocess, "run", run)
    assert helpers.find_kpclientd_container() == "mix-kpclientd-1"
    assert run.call_args.args[0][0] == engine
    assert run.call_args.kwargs["timeout"] == 10
    helpers.podman(["restart", "mix-kpclientd-1"])
    assert run.call_args.args[0] == [engine, "restart", "mix-kpclientd-1"]


def test_engine_selection_does_not_change_the_existing_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KATZENQT_CONTAINER_ENGINE", raising=False)
    assert helpers._engine_command(["ps"]) == ["podman", "ps"]
    monkeypatch.setenv("KATZENQT_CONTAINER_ENGINE", "sh -c")
    with pytest.raises(ValueError):
        helpers._engine_command(["ps"])


def test_workflows_use_the_resolved_path_everywhere() -> None:
    text = (ROOT / ".github/workflows/test-integration-namenlos.yml").read_text()
    assert text.count("path: ${{ env.KATZENPOST_CI_PATH }}\n") == 3
    assert 'cd katzenpost/cmd' not in text
    assert 'cd "$KATZENPOST_DIR/cmd/kpclientd"' in text
    assert 'cd katzenqt/katzenpost/docker' not in text
    assert text.count('working-directory: ${{ env.KATZENPOST_DIR }}/docker') == 3
    assert 'KATZENPOST_DIR }}/docker/mixnet-alpine' in text
    assert "Stop the namenlos client" in text
    assert "trap - EXIT" in text


def test_local_run_without_shared_paths_stops_before_checkout(tmp_path: Path) -> None:
    command = (
        'test() { if [[ "$1" == -S ]]; then return 0; '
        'else builtin test "$@"; fi; }; '
        'mountpoint() { return 1; }; source "$1"'
    )
    environment = dict(
        os.environ, ACT="true", GITHUB_JOB="epoch-integration",
        GITHUB_WORKSPACE=str(tmp_path), GITHUB_ENV=str(tmp_path / "env"),
    )
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(ROOT / "tools/ci-paths.sh")],
        env=environment, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert "use make ci-local" in result.stderr
    assert not (tmp_path / "env").exists()
