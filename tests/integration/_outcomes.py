from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal

RoleStatus = Literal["passed", "deadline", "failed"]


class DeliveryDeadline(Exception):
    pass


@dataclass(frozen=True)
class RoleResult:
    status: RoleStatus
    returncode: int


def role_result(
    returncode: int, deadline_code: int | None, had_exception: bool,
) -> RoleResult:
    if had_exception:
        return RoleResult("failed", returncode if returncode else 1)
    if returncode == 0:
        return RoleResult("passed", 0)
    if returncode == deadline_code:
        return RoleResult("deadline", returncode)
    return RoleResult("failed", returncode)


def result_path(stderr: Path) -> Path:
    return stderr.with_suffix(".result.json")


def load_result(path: Path, returncode: int | None) -> RoleResult:
    data: object = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid role result: {path}")
    if type(data.get("version")) is not int or data.get("version") != 1:
        raise ValueError(f"invalid role result: {path}")
    status = data.get("status")
    code = data.get("returncode")
    if status not in ("passed", "deadline", "failed"):
        raise ValueError(f"invalid role status: {path}")
    if type(code) is not int or code != returncode:
        raise ValueError(f"role exit status disagrees with report: {path}")
    if (status == "passed") != (code == 0):
        raise ValueError(f"inconsistent role result: {path}")
    if status == "passed":
        return RoleResult("passed", code)
    if status == "deadline":
        if code not in (1, 3, 4):
            raise ValueError(f"invalid deadline exit status: {path}")
        return RoleResult("deadline", code)
    return RoleResult("failed", code)


def check_roles(roles: list[tuple[int | None, Path]]) -> None:
    results = [
        (load_result(result_path(stderr), code), stderr)
        for code, stderr in roles
    ]
    failures = [
        str(path) + "\n" + (
            path.read_text(encoding="utf-8", errors="replace")[-8192:]
            if path.is_file() else ""
        )
        for result, path in results if result.status == "failed"
    ]
    if failures:
        raise AssertionError("role failed; see " + ", ".join(failures))
    deadlines = [
        str(path) for result, path in results if result.status == "deadline"
    ]
    if deadlines:
        raise DeliveryDeadline(
            "delivery deadline expired; see " + ", ".join(deadlines)
        )
