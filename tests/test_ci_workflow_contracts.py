from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/test-integration-namenlos.yml"


def _jobs(text: str) -> dict[str, str]:
    starts = list(re.finditer(r"^  ([\w-]+):\s*$", text, re.M))
    stops = [match.start() for match in starts[1:]] + [len(text)]
    return {
        match[1]: text[match.end():stop]
        for match, stop in zip(starts, stops, strict=True)
        if "integration" in match[1]
    }


def _step(job: str, name: str) -> str:
    blocks = re.split(r"^      - name: ", job, flags=re.M)[1:]
    return next(block for block in blocks if block.splitlines()[0] == name)


def test_step_outcome_references_have_producers_in_the_same_job() -> None:
    for name, job in _jobs(WORKFLOW.read_text(encoding="utf-8")).items():
        declared = set(re.findall(r"^        id: ([\w-]+)\s*$", job, re.M))
        referenced = set(re.findall(r"\bsteps\.([\w-]+)\.", job))
        assert referenced <= declared, (name, referenced - declared)


def test_serial_phase_runs_after_parallel_failure_only() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    serial = _step(
        jobs["docker-integration"], "Run serial watchdog integration tests",
    )
    condition = next(
        line.strip() for line in serial.splitlines()
        if line.strip().startswith("if:")
    )
    assert "!cancelled()" in condition
    assert "steps.start_mixnet.outcome == 'success'" in condition
    assert "steps.install_deps" not in condition
    assert "success()" not in condition


def test_live_failure_is_classified_before_allowing_fallback() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    live = jobs["namenlos-integration"]
    assert "continue-on-error: true" in _step(live, "Run integration tests")
    classification = _step(live, "Check the namenlos result")
    assert "continue-on-error" not in classification
    assert "!cancelled()" in classification
    assert "steps.tests.outputs.exit_code" in classification
    assert "--report=integration-results/namenlos.json" in classification
    assert "steps.result.outputs.verdict" in live
    fallback = jobs["docker-integration"]
    assert "!cancelled()" in fallback
    assert (
        "needs.namenlos-integration.outputs.verdict != 'passed'" in fallback
    )


def test_listener_configuration_errors_are_not_advisory() -> None:
    live = _jobs(WORKFLOW.read_text(encoding="utf-8"))["namenlos-integration"]
    config = _step(live, "Configure the namenlos listener")
    assert "continue-on-error" not in config
    assert "client.toml Listen block moved" in config
    probe = _step(live, "Start kpclientd against namenlos")
    assert "continue-on-error: true" in probe
    assert "verdict=deadline" in probe
    assert "kill -0" in probe
    assert "python3" not in probe
    assert "if: always()" in _step(live, "Upload logs")


def test_live_tests_declare_epoch_without_docker() -> None:
    job = _jobs(WORKFLOW.read_text(encoding="utf-8"))["namenlos-integration"]
    tests = _step(job, "Run integration tests")
    assert 'KQT_INTEGRATION_TARGET: "namenlos"' in tests
    assert 'KQT_EPOCH_DURATION_S: "1200"' in tests


def test_result_job_runs_after_skipped_or_failed_dependencies() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    result = jobs["integration-result"]
    assert re.search(r"^    if: always\(\)\s*$", result, re.M)
    assert (
        "needs: [namenlos-integration, docker-integration, epoch-integration]"
        in result
    )
    assert "continue-on-error" not in result


def test_docker_is_optional_but_epoch_coverage_is_not() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    fallback = jobs["docker-integration"]
    condition = next(
        line.strip() for line in fallback.splitlines()
        if line.startswith("    if:")
    )
    assert condition == (
        "if: ${{ !cancelled() && "
        "(needs.namenlos-integration.result != 'success' || "
        "needs.namenlos-integration.outputs.verdict != 'passed') }}"
    )
    epoch = jobs["epoch-integration"]
    assert not re.search(r"^    (if|needs|continue-on-error):", epoch, re.M)
    assert "-m epoch_driven" in epoch
