from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Literal

Outcome = Literal["passed", "deadline", "failed"]


def pytest_outcome(data: object, exit_code: str, step: str) -> Outcome:
    if not isinstance(data, dict):
        return "failed"
    if type(data.get("version")) is not int or data.get("version") != 1:
        return "failed"
    code = data.get("exit_code")
    cases = data.get("cases")
    if type(code) is not int or str(code) != exit_code:
        return "failed"
    if code not in (0, 1) or step != ("success" if code == 0 else "failure"):
        return "failed"
    if not isinstance(cases, list) or not cases:
        return "failed"
    if any(case not in ("passed", "deadline") for case in cases):
        return "failed"
    expected = "deadline" if "deadline" in cases else "passed"
    if code != (1 if expected == "deadline" else 0):
        return "failed"
    if data.get("outcome") != expected:
        return "failed"
    return "deadline" if expected == "deadline" else "passed"


def live_outcome(
    connect: str, probe: str, tests: str, exit_code: str, data: object,
) -> Outcome:
    if connect == "failure" and probe == "deadline" and tests == "skipped":
        return "deadline"
    if connect != "success" or probe != "passed":
        return "failed"
    return pytest_outcome(data, exit_code, tests)


def integration_passed(
    live_job: str, live: str, docker: str, epoch: str,
) -> bool:
    if live_job != "success" or epoch != "success":
        return False
    if live == "passed":
        return docker in ("success", "skipped")
    return live == "deadline" and docker == "success"


def publish(outcome: Outcome, text: str) -> int:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="ascii") as handle:
            handle.write(f"verdict={outcome}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="ascii") as handle:
            handle.write(text + "\n")
    sys.stdout.write(text + "\n")
    return 1 if outcome == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live")
    for name in ("connect", "probe", "tests", "exit-code"):
        live.add_argument(f"--{name}", default="")
    live.add_argument("--report", required=True, type=Path)
    final = commands.add_parser("final")
    for name in ("live-job", "live", "docker", "epoch"):
        final.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    if args.command == "live":
        data: object = None
        try:
            data = json.loads(args.report.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeError):
            pass
        outcome = live_outcome(
            args.connect, args.probe, args.tests, args.exit_code, data,
        )
        text = f"Namenlos: {outcome}."
        if outcome == "deadline":
            text += (
                " The live run did not pass; full Docker validation is required."
            )
        return publish(outcome, text)
    passed = integration_passed(
        args.live_job, args.live, args.docker, args.epoch,
    )
    text = (
        f"Namenlos: {args.live or 'missing'} (job: {args.live_job}).\n"
        f"Docker fallback: {args.docker}.\nEpoch tests: {args.epoch}."
    )
    return publish("passed" if passed else "failed", text)


if __name__ == "__main__":
    raise SystemExit(main())
