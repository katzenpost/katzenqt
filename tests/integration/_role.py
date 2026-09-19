from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import re
import sys
from types import TracebackType

from tests.integration._outcomes import role_result

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]
EmptyExcInfo = tuple[None, None, None]


def deadline_code(record: logging.LogRecord) -> int | None:
    if record.name != "katzen.headless":
        return None
    if record.funcName in ("_action_read", "_action_read_file"):
        return 1 if record.msg == "TIMEOUT" else None
    if record.funcName == "_send_one_gcm":
        return (
            3 if record.msg == "send timed out waiting for SentLog" else None
        )
    if record.funcName == "_action_multi_send":
        return 3 if record.msg == "multi-send timed out" else None
    if record.funcName == "_action_chat_session":
        match = re.match(
            r"^STEP_FAIL:[0-9]+:(send|read)-timeout:", str(record.msg),
        )
        return (3 if match[1] == "send" else 4) if match else None
    if record.funcName in ("_action_tally_vote", "_action_tally_close"):
        if record.msg == "survey %s not received within %.0fs":
            return 1
        if record.msg in (
            "vote send timed out for survey %s",
            "close send timed out for survey %s",
        ):
            return 3
    if record.funcName == "_action_tally_result":
        return (
            1 if record.msg == "tally result timed out for survey %s" else None
        )
    return None


@dataclass
class Observation:
    deadline: int | None = None
    failed: bool = False

    def observe(self, record: logging.LogRecord) -> None:
        if record.exc_info and record.exc_info[1] is not None:
            if not isinstance(record.exc_info[1], asyncio.CancelledError):
                self.failed = True
        code = deadline_code(record)
        if code is not None:
            self.deadline = code


def run_observed(
    invoke: Callable[[list[str]], int], args: list[str], report: Path,
) -> int:
    observation = Observation()
    original = logging.getLogRecordFactory()

    def record(
        name: str, level: int, pathname: str, lineno: int, msg: object,
        values: tuple[object, ...] | Mapping[str, object] | None,
        exc_info: ExcInfo | EmptyExcInfo | None,
        func: str | None = None, sinfo: str | None = None,
        **kwargs: object,
    ) -> logging.LogRecord:
        entry = original(
            name, level, pathname, lineno, msg, values, exc_info,
            func, sinfo, **kwargs,
        )
        observation.observe(entry)
        return entry

    code = 1
    logging.setLogRecordFactory(record)
    try:
        code = invoke(args)
    except BaseException:
        observation.failed = True
        raise
    finally:
        logging.setLogRecordFactory(original)
        result = role_result(code, observation.deadline, observation.failed)
        report.write_text(
            json.dumps({"version": 1, **asdict(result)}, ensure_ascii=True)
            + "\n",
            encoding="ascii",
        )
    return result.returncode


def invoke(args: list[str]) -> int:
    from katzenqt.integration_runner import main

    return main(args)


def main(args: list[str]) -> int:
    if len(args) < 2:
        raise ValueError("expected result path and role arguments")
    return run_observed(invoke, args[1:], Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
