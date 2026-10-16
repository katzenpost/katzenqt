"""Report dependency pins that upstream has moved past. Advisory: this
never fails, because what is newest is a judgement and an upgrade is a
decision to take deliberately.

Each pin is resolved to the tag that carries it, when one does, and then
to the tags whose commits are newer. A pin on an untagged commit, or a
repository with no tags, is reported as such rather than guessed at.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "packaging" / "debian" / "targets.mk"

GIT_PINS = (
    ("katzenpost", "katzenpost/katzenpost", ROOT / "Makefile",
     "KATZENPOST_REV"),
    ("thin_client", "katzenpost/thin_client", TARGETS, "THINCLIENT_REV"),
    ("hpqc", "katzenpost/hpqc", TARGETS, "HPQC_REV"),
    ("rustic_audio", "katzenpost/Rustic_Audio_PyO3", TARGETS,
     "RUSTIC_AUDIO_REV"),
    ("pycrdt", "y-crdt/pycrdt", TARGETS, "PYCRDT_REV"),
)
PYPI_PINS = ("pycrdt", "pyside6", "katzenpost-thinclient")


def _get(url: str) -> object | None:
    """The JSON at ``url``, or None if it cannot be had.

    GitHub allows 60 unauthenticated calls an hour, which this exhausts,
    so GITHUB_TOKEN is used when the environment offers one. Without it
    the report says "unreachable" rather than guessing.
    """
    request = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _rev(path: Path, var: str) -> str | None:
    found = re.search(
        rf"(?m)^{re.escape(var)} := ([0-9a-f]{{40}})$", path.read_text()
    )
    return found.group(1) if found else None


def _pin_constraint(name: str) -> str | None:
    found = re.search(
        rf'"{re.escape(name)}~=([0-9][0-9.]*)"',
        (ROOT / "pyproject.toml").read_text(),
    )
    return found.group(1) if found else None


def _git_report() -> int:
    behind = 0
    for label, repo, path, var in GIT_PINS:
        pin = _rev(path, var)
        if pin is None:
            print(f"  {label:<13} {var} not found in {path.name}")
            continue
        tags = _get(f"https://api.github.com/repos/{repo}/tags?per_page=100")
        if not isinstance(tags, list):
            print(f"  {label:<13} {pin[:9]} (upstream unreachable)")
            continue
        here = {t["commit"]["sha"]: t["name"] for t in tags}.get(pin)
        where = here or f"{pin[:9]} untagged"
        cmp = _get(f"https://api.github.com/repos/{repo}/compare/{pin}...HEAD")
        if not isinstance(cmp, dict) or "ahead_by" not in cmp:
            print(f"  {label:<13} {where}")
            continue
        ahead = cmp["ahead_by"]
        if ahead:
            behind += 1
            print(f"  {label:<13} {where}  {ahead} commits behind HEAD")
        else:
            print(f"  {label:<13} {where}  at HEAD")
    return behind


def _pypi_report() -> int:
    behind = 0
    for name in PYPI_PINS:
        data = _get(f"https://pypi.org/pypi/{name}/json")
        latest = (
            data["info"]["version"] if isinstance(data, dict) else None
        )
        pinned = _pin_constraint(name) or _pin_exact(name)
        if pinned is None:
            print(f"  {name:<13} not pinned in pyproject.toml")
            continue
        if latest is None:
            print(f"  {name:<13} {pinned} (PyPI unreachable)")
        elif latest == pinned:
            print(f"  {name:<13} {pinned}")
        else:
            behind += 1
            print(f"  {name:<13} {pinned}  newer: {latest}")
    return behind


def _pin_exact(name: str) -> str | None:
    found = re.search(
        rf'"{re.escape(name)}==([0-9][0-9.]*)"',
        (ROOT / "pyproject.toml").read_text(),
    )
    return found.group(1) if found else None


def main() -> int:
    print("git pins:")
    behind = _git_report()
    print("pypi pins:")
    behind += _pypi_report()
    print(f"\n{behind} pin(s) upstream has moved past. Advisory only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
