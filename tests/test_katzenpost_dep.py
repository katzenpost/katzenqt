import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CI = ROOT / "packaging" / "debian" / "ci.sh"
DEP_DEB = ROOT / "packaging" / "container" / "dep-deb.sh"


def test_katzenqt_depends_on_the_katzenpost_package() -> None:
    control = (ROOT / "debian" / "control").read_text()
    assert re.search(r"^\s*katzenpost,\s*$", control, re.MULTILINE)
    assert not re.search(r"^\s*kpclientd,\s*$", control, re.MULTILINE)


def test_nothing_builds_kpclientd_here_any_more() -> None:
    container = ROOT / "packaging" / "container"
    assert not (container / "kpclientd-build.sh").exists()
    assert not (container / "build-kpclientd.sh").exists()
    assert not (container / "Containerfile.kpclientd").exists()
    assert not (ROOT / "packaging" / "debian" / "kpclientd").exists()
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    assert "kpclientd" not in mk
    rules = (ROOT / "debian" / "rules").read_text()
    assert "kpclientd" not in rules


def test_dependency_debs_come_from_their_own_make_target() -> None:
    assert os.access(DEP_DEB, os.X_OK)
    body = DEP_DEB.read_text()
    assert "deb-build" in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "switch --detach" in body
    assert "kpclientd" not in body
    assert "katzenpost" not in body


def test_ci_builds_and_installs_the_katzenpost_deb() -> None:
    ci = CI.read_text()
    assert "packaging/container/dep-deb.sh" in ci
    assert '"$KATZENPOST_URL" "$KATZENPOST_REV"' in ci
    assert '"$debs"/katzenpost_*.deb' in ci
