from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs" / "debian.md", ROOT / "docs" / "container.md")
MAKEFILES = (
    ROOT / "Makefile",
    ROOT / "ci.mk",
    ROOT / "packaging" / "debian" / "Makefile",
    ROOT / "packaging" / "debian" / "targets.mk",
)
TARGET = re.compile(r"make (?:-C packaging/debian )?([a-z0-9][a-z0-9-]*)")
NAMED_PATH = re.compile(
    r"`((?:debian|packaging|docs|\.github)/[A-Za-z0-9_./-]+"
    r"|[A-Za-z0-9_.-]+\.(?:sh|mk|yml|toml|env))`",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _declared_targets() -> set[str]:
    found: set[str] = set()
    for path in MAKEFILES:
        for line in _text(path).splitlines():
            if match := re.match(r"([a-z0-9][a-z0-9_.-]*):", line):
                found.add(match.group(1))
    return found


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_make_target_the_doc_names_exists(doc: Path) -> None:
    declared = _declared_targets()
    named = set(TARGET.findall(_text(doc)))
    assert named, f"{doc.name} names no make target"
    assert named <= declared, sorted(named - declared)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_path_the_doc_names_exists(doc: Path) -> None:
    missing = []
    for name in sorted(set(NAMED_PATH.findall(_text(doc)))):
        candidate = name if isinstance(name, str) else name[0]
        if (ROOT / candidate).exists():
            continue
        if any(ROOT.rglob(Path(candidate).name)):
            continue
        missing.append(candidate)
    assert missing == []


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_the_doc_is_seven_bit_and_wrapped(doc: Path) -> None:
    for number, line in enumerate(_text(doc).splitlines(), start=1):
        assert line.isascii(), f"{doc.name}:{number} is not ascii"
        assert len(line) <= 78, f"{doc.name}:{number} is {len(line)} columns"


def test_the_doc_names_the_distros_that_can_be_built_locally() -> None:
    overrides = ROOT / "packaging" / "container" / "overrides"
    local = sorted(p.stem for p in overrides.glob("*.env"))
    text = _text(ROOT / "docs" / "container.md")
    for distro in local:
        assert f"{distro}.env" in text
    assert local == ["debian-13", "ubuntu-26.04"]
