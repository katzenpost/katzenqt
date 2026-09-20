import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.integration_result import (
    integration_passed, live_outcome, pytest_outcome,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/test-integration-namenlos.yml"


def _report(outcome: str, code: int, cases: list[str]) -> dict[str, object]:
    return {
        "version": 1, "outcome": outcome, "exit_code": code, "cases": cases,
    }


@pytest.mark.parametrize("data, code, step, expected", [
    (_report("passed", 0, ["passed"]), "0", "success", "passed"),
    (_report("deadline", 1, ["passed", "deadline"]),
     "1", "failure", "deadline"),
    (_report("deadline", 1, ["deadline", "failed"]),
     "1", "failure", "failed"),
    (_report("passed", 0, []), "0", "success", "failed"),
    (_report("passed", 0, ["skipped"]), "0", "success", "failed"),
    (_report("deadline", 1, ["deadline"]), "1", "success", "failed"),
    (_report("deadline", 1, ["deadline"]), "0", "success", "failed"),
    (_report("passed", 0, ["passed"]), "", "success", "failed"),
    (_report("deadline", 3, ["deadline"]), "3", "failure", "failed"),
    ({**_report("passed", 0, ["passed"]), "version": True},
     "0", "success", "failed"),
    (None, "1", "failure", "failed"),
])
def test_pytest_result_requires_complete_matching_evidence(
    data: object, code: str, step: str, expected: str,
) -> None:
    assert pytest_outcome(data, code, step) == expected


@pytest.mark.parametrize("connect, probe, tests, expected", [
    ("failure", "deadline", "skipped", "deadline"),
    ("failure", "", "skipped", "failed"),
    ("skipped", "", "skipped", "failed"),
    ("success", "passed", "skipped", "failed"),
    ("failure", "failed", "skipped", "failed"),
    ("failure", "deadline", "failure", "failed"),
])
def test_probe_failures_are_not_all_network_failures(
    connect: str, probe: str, tests: str, expected: str,
) -> None:
    assert live_outcome(connect, probe, tests, "", None) == expected


def test_all_final_job_combinations() -> None:
    states = ("success", "failure", "skipped", "cancelled", "")
    for live_job, live, docker, epoch in itertools.product(
        states, ("passed", "deadline", "failed", ""), states, states,
    ):
        expected = (
            live_job == epoch == "success"
            and ((live == "passed" and docker in ("success", "skipped"))
                 or (live == "deadline" and docker == "success"))
        )
        assert integration_passed(live_job, live, docker, epoch) == expected


def test_hard_live_failure_cannot_be_overridden_by_docker() -> None:
    assert not integration_passed("failure", "failed", "success", "success")
    assert not integration_passed("success", "failed", "success", "success")


def test_final_command_publishes_both_lane_results(tmp_path: Path) -> None:
    output, summary = tmp_path / "output", tmp_path / "summary"
    run = subprocess.run(
        [sys.executable, "-m", "tools.integration_result", "final",
         "--live-job=success", "--live=deadline", "--docker=success",
         "--epoch=success"],
        cwd=ROOT, env=dict(os.environ, GITHUB_OUTPUT=str(output),
                           GITHUB_STEP_SUMMARY=str(summary)),
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr
    assert output.read_text() == "verdict=passed\n"
    assert "Namenlos: deadline" in summary.read_text()
    assert "Docker fallback: success" in summary.read_text()


def test_missing_live_report_is_a_hard_failure(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "tools.integration_result", "live",
         "--connect=success", "--probe=passed", "--tests=failure",
         "--exit-code=1", f"--report={tmp_path / 'missing.json'}"],
        cwd=ROOT, env=dict(os.environ,
                           GITHUB_OUTPUT=str(tmp_path / "output"),
                           GITHUB_STEP_SUMMARY=str(tmp_path / "summary")),
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 1


def test_workflow_has_a_required_result_and_separate_epoch_job() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "  integration-result:" in text
    assert (
        "needs: [namenlos-integration, docker-integration, epoch-integration]"
        in text
    )
    epoch = text.split("  epoch-integration:", 1)[1].split(
        "  integration-result:", 1,
    )[0]
    assert "    needs:" not in epoch
    assert "-m epoch_driven" in epoch
    assert "continue-on-error" not in epoch


def test_live_run_removes_the_previous_report_first() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    clear = text.index("rm -f integration-results/namenlos.json")
    execute = text.index(".venv/bin/python -m pytest")
    assert clear < execute


@pytest.mark.parametrize("live_job, live, docker, epoch, expected", [
    ("success", "passed", "skipped", "success", 0),
    ("success", "deadline", "skipped", "success", 1),
    ("failure", "failed", "success", "success", 1),
    ("success", "passed", "skipped", "skipped", 1),
])
def test_final_command_checks_skipped_lanes(
    tmp_path: Path, live_job: str, live: str, docker: str,
    epoch: str, expected: int,
) -> None:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    run = subprocess.run(
        [sys.executable, "-m", "tools.integration_result", "final",
         f"--live-job={live_job}", f"--live={live}",
         f"--docker={docker}", f"--epoch={epoch}"],
        cwd=ROOT, env=dict(os.environ, GITHUB_OUTPUT=str(output),
                           GITHUB_STEP_SUMMARY=str(summary)),
        capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == expected, run.stdout + run.stderr
    verdict = "passed" if expected == 0 else "failed"
    assert output.read_text(encoding="ascii") == f"verdict={verdict}\n"
    text = summary.read_text(encoding="ascii")
    assert f"Docker fallback: {docker}." in text
    assert f"Epoch tests: {epoch}." in text
