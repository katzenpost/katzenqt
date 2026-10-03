import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CI = ROOT / "packaging" / "debian" / "ci.sh"
SCRIPT = ROOT / "packaging" / "container" / "kpclientd-build.sh"


def _group(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    assert match, pattern
    return match.group(1)


def test_the_kpclientd_deb_is_built_by_katzenpost() -> None:
    assert os.access(SCRIPT, os.X_OK)
    body = SCRIPT.read_text()
    assert "KATZENPOST_REV" in body
    assert "packaging/debian/ci.sh" in body
    assert "DEBS_DIR" in body


def test_katzenqt_no_longer_builds_the_daemon_itself() -> None:
    body = SCRIPT.read_text()
    assert "go build" not in body
    assert "dpkg-deb" not in body
    assert not (ROOT / "packaging" / "debian" / "kpclientd").exists()


def test_the_unit_and_the_config_come_from_the_dependency() -> None:
    rules = (ROOT / "debian" / "rules").read_text()
    assert "usr/lib/systemd/user" not in rules
    assert "kpclientd.service" not in rules
    smoke = (ROOT / "packaging" / "debian" / "smoke.sh").read_text()
    assert "test -f /usr/lib/systemd/user/kpclientd.service" in smoke
    assert "test -f /etc/kpclientd/client.toml" in smoke


def test_make_target_builds_kpclientd_in_container() -> None:
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    recipe = _group(r"^container-kpclientd:.*\n((?:\t.*\n)+)", mk)
    assert "../container/build-kpclientd.sh" in recipe


def test_katzenqt_depends_on_the_katzenpost_package() -> None:
    """kpclientd ships inside katzenpost, which carries its version."""
    control = (ROOT / "debian" / "control").read_text()
    assert re.search(r"^\s*katzenpost,\s*$", control, re.MULTILINE)
    assert not re.search(r"^\s*kpclientd,\s*$", control, re.MULTILINE)


def test_ci_builds_the_kpclientd_deb() -> None:
    wf = (ROOT / ".github" / "workflows" / "deb.yml").read_text()
    assert "make deb-ci" in wf
    assert "packaging/container/kpclientd-build.sh" in CI.read_text()


def test_the_installed_deb_is_not_pinned_to_one_version() -> None:
    body = CI.read_text()
    assert "kpclientd_0.0.1_" not in body
    assert "katzenpost_*.deb" in body
