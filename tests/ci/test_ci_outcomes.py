import asyncio
import json
import logging
from pathlib import Path
import subprocess
import sys

import pytest

from tests.integration._outcomes import (
    DeliveryDeadline, RoleResult, check_roles, load_result, result_path,
    role_result,
)
from tests.integration._role import Observation, deadline_code, run_observed
from tests.integration._report import CaseStatus, summarize


@pytest.mark.parametrize("code, deadline, failed, expected", [
    (0, None, False, RoleResult("passed", 0)),
    (3, 3, False, RoleResult("deadline", 3)),
    (1, None, False, RoleResult("failed", 1)),
    (1, 3, False, RoleResult("failed", 1)),
    (0, 3, True, RoleResult("failed", 1)),
    (3, 3, True, RoleResult("failed", 3)),
    (-9, None, False, RoleResult("failed", -9)),
])
def test_role_result(
    code: int, deadline: int | None, failed: bool, expected: RoleResult,
) -> None:
    assert role_result(code, deadline, failed) == expected


def _entry(function: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "katzen.headless", logging.INFO, "_actions.py", 1, message,
        (), None, func=function,
    )


@pytest.mark.parametrize("function, message, expected", [
    ("_action_read", "TIMEOUT", 1),
    ("_action_read_file", "TIMEOUT", 1),
    ("_send_one_gcm", "send timed out waiting for SentLog", 3),
    ("_action_chat_session", "STEP_FAIL:2:read-timeout:hello", 4),
    ("_action_chat_session", "STEP_FAIL:0:send-timeout:hello", 3),
    ("_action_chat_session", "STEP_FAIL:0:unknown-step:hello", None),
    ("_action_read", "RECV=TIMEOUT", None),
    ("unrelated", "TIMEOUT", None),
    ("_action_tally_vote", "could not apply vote to survey %s", None),
])
def test_only_explicit_deadline_records_are_classified(
    function: str, message: str, expected: int | None,
) -> None:
    assert deadline_code(_entry(function, message)) == expected


def test_foreign_logger_cannot_supply_deadline() -> None:
    record = _entry("_action_read", "TIMEOUT")
    record.name = "another.module"
    assert deadline_code(record) is None


@pytest.mark.parametrize(
    "failure", [RuntimeError("bug"), TimeoutError("bug")],
)
def test_python_exceptions_are_not_network_deadlines(
    tmp_path: Path, failure: Exception,
) -> None:
    path = tmp_path / "result.json"
    original = logging.getLogRecordFactory()

    def invoke(args: list[str]) -> int:
        raise failure

    with pytest.raises(type(failure)) as caught:
        run_observed(invoke, [], path)
    assert caught.value is failure
    assert logging.getLogRecordFactory() is original
    assert load_result(path, 1) == RoleResult("failed", 1)


def test_logged_background_exception_overrides_clean_exit(
    tmp_path: Path,
) -> None:
    def invoke(args: list[str]) -> int:
        try:
            raise RuntimeError("background task failed")
        except RuntimeError:
            logging.getLogger("katzen.network").exception("task died")
        return 0

    path = tmp_path / "result.json"
    assert run_observed(invoke, [], path) == 1
    assert load_result(path, 1).status == "failed"


def test_normal_cancellation_is_not_a_background_failure() -> None:
    entry = _entry("_action_read", "cancelled")
    entry.exc_info = (asyncio.CancelledError, asyncio.CancelledError(), None)
    observed = Observation()
    observed.observe(entry)
    assert not observed.failed


def _save(path: Path, status: str, code: int) -> None:
    path.write_text(json.dumps({
        "version": 1, "status": status, "returncode": code,
    }), encoding="ascii")


def test_error_in_one_role_wins_over_other_role_deadline(
    tmp_path: Path,
) -> None:
    a, b = tmp_path / "alice.err", tmp_path / "bob.err"
    _save(result_path(a), "deadline", 3)
    _save(result_path(b), "failed", 1)
    with pytest.raises(AssertionError, match="role failed"):
        check_roles([(3, a), (1, b)])


def test_completed_role_deadline_has_its_own_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alice.err"
    _save(result_path(path), "deadline", 3)
    with pytest.raises(DeliveryDeadline):
        check_roles([(3, path)])


@pytest.mark.parametrize("status, code, observed", [
    ("passed", 0, 1), ("deadline", 3, 0), ("passed", 3, 3),
    ("unknown", 1, 1), ("failed", 0, 0),
])
def test_inconsistent_role_report_fails_closed(
    tmp_path: Path, status: str, code: int, observed: int,
) -> None:
    path = tmp_path / "report.json"
    _save(path, status, code)
    with pytest.raises(ValueError):
        load_result(path, observed)


@pytest.mark.parametrize("status, cases, expected", [
    (0, ["passed"], "passed"),
    (1, ["passed", "deadline"], "deadline"),
    (1, ["deadline", "failed"], "failed"),
    (1, ["passed"], "failed"),
    (0, ["deadline"], "failed"),
    (0, ["skipped"], "failed"),
    (0, [], "failed"),
    (2, ["deadline"], "failed"),
    (3, ["passed"], "failed"),
    (5, [], "failed"),
])
def test_session_result(
    status: int, cases: list[CaseStatus], expected: str,
) -> None:
    assert summarize(status, cases) == expected


@pytest.mark.parametrize("body, expected", [
    ("pass", "passed"),
    ("raise DeliveryDeadline('delivery expired')", "deadline"),
    ("raise TimeoutError('unexpected Python timeout')", "failed"),
    ("raise RuntimeError('unexpected exception')", "failed"),
    ("assert False", "failed"),
    ("pytest.skip('not run')", "failed"),
])
def test_real_pytest_reports_preserve_failure_exit_code(
    tmp_path: Path, body: str, expected: str,
) -> None:
    case = tmp_path / "test_case.py"
    case.write_text(
        "import pytest\n"
        "from tests.integration._outcomes import DeliveryDeadline\n"
        f"def test_case():\n    {body}\n", encoding="ascii",
    )
    path = tmp_path / "report.json"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", "-o", "addopts=",
         "-p", "tests.integration._report", f"--integration-report={path}",
         "-q", str(case)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30,
    )
    assert path.exists(), run.stdout + run.stderr
    data = json.loads(path.read_text(encoding="ascii"))
    assert data["outcome"] == expected, run.stdout + run.stderr
    assert data["exit_code"] == run.returncode
    if expected == "deadline":
        assert run.returncode == 1
