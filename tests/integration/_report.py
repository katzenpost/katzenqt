from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Literal

import pytest

from tests.integration._outcomes import DeliveryDeadline

CaseStatus = Literal["passed", "deadline", "failed", "skipped"]
_PROPERTY = "katzenqt.integration.result"


def summarize(exitstatus: int, cases: list[CaseStatus]) -> str:
    if exitstatus not in (0, 1) or not cases:
        return "failed"
    if "failed" in cases or "skipped" in cases:
        return "failed"
    if "deadline" in cases:
        return "deadline" if exitstatus == 1 else "failed"
    return "passed" if exitstatus == 0 else "failed"


@dataclass(eq=False)
class Reports:
    path: Path
    cases: list[CaseStatus] = field(default_factory=list)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None],
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        if report.failed:
            status = "failed"
            if (
                report.when == "call" and call.excinfo is not None
                and isinstance(call.excinfo.value, DeliveryDeadline)
            ):
                status = "deadline"
            report.user_properties.append((_PROPERTY, status))
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            properties = dict(report.user_properties)
            self.cases.append(
                "deadline" if properties.get(_PROPERTY) == "deadline"
                and report.when == "call" else "failed"
            )
        elif report.skipped:
            self.cases.append("skipped")
        elif report.when == "call":
            self.cases.append("passed")

    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: int,
    ) -> None:
        if hasattr(session.config, "workerinput"):
            return
        data = {
            "version": 1, "exit_code": int(exitstatus),
            "outcome": summarize(int(exitstatus), self.cases),
            "cases": self.cases,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=True) + "\n", encoding="ascii",
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--integration-report", default=None)


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("integration_report")
    if path is not None:
        config.pluginmanager.register(Reports(Path(path)), "kqt-results")
