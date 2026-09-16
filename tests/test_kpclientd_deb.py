import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CI = ROOT / "packaging" / "debian" / "ci.sh"


def _group(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    assert match, pattern
    return match.group(1)


def test_kpclientd_control_declares_the_package() -> None:
    control = (
        ROOT / "packaging" / "debian" / "kpclientd" / "control"
    ).read_text()
    assert "Package: kpclientd" in control
    assert "Architecture: amd64" in control
    assert "Maintainer: Katzenpost" in control


def test_build_script_uses_pinned_rev_and_makes_a_deb() -> None:
    script = ROOT / "packaging" / "container" / "kpclientd-build.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "KATZENPOST_REV" in body
    assert "go build" in body
    assert "dpkg-deb --build" in body
    assert "/usr/bin/kpclientd" in body
    assert "GOFLAGS=-trimpath" in body


def test_make_target_builds_kpclientd_in_container() -> None:
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    recipe = _group(r"^container-kpclientd:.*\n((?:\t.*\n)+)", mk)
    assert "../container/build-kpclientd.sh" in recipe


def test_katzenqt_depends_on_kpclientd_package() -> None:
    control = (ROOT / "debian" / "control").read_text()
    assert re.search(r"^\s*kpclientd,\s*$", control, re.MULTILINE)


def test_ci_builds_the_kpclientd_deb() -> None:
    wf = (ROOT / ".github" / "workflows" / "deb.yml").read_text()
    assert "make deb-ci" in wf
    assert "packaging/container/kpclientd-build.sh" in CI.read_text()


def test_build_script_derives_arch_and_pins_source_date_epoch() -> None:
    body = (
        ROOT / "packaging" / "container" / "kpclientd-build.sh"
    ).read_text()
    assert "dpkg --print-architecture" in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "kpclientd_0.0.1_amd64.deb" not in body
