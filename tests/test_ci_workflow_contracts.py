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
