import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_kpclientd_control_declares_the_package():
    control = (ROOT / "packaging" / "debian" / "kpclientd" / "control").read_text()
    assert "Package: kpclientd" in control
    assert "Architecture: amd64" in control
    assert "Maintainer: Katzenpost" in control


def test_build_script_uses_pinned_rev_and_makes_a_deb():
    script = ROOT / "packaging" / "container" / "kpclientd-build.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "KATZENPOST_REV" in body
    assert "go build" in body
    assert "dpkg-deb --build" in body
    assert "/usr/bin/kpclientd" in body
    assert "GOFLAGS=-trimpath" in body


def test_make_target_builds_kpclientd_in_container():
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    recipe = re.search(
        r"^container-kpclientd:.*\n((?:\t.*\n)+)", mk, re.MULTILINE
    ).group(1)
    assert "../container/build-kpclientd.sh" in recipe


def test_katzenqt_depends_on_kpclientd_package():
    control = (ROOT / "debian" / "control").read_text()
    assert re.search(r"^\s*kpclientd,\s*$", control, re.MULTILINE)


def test_ci_builds_the_kpclientd_deb():
    wf = (ROOT / ".github" / "workflows" / "deb-multi-os.yml").read_text()
    assert "packaging/container/kpclientd-build.sh" in wf


def test_build_script_derives_arch_and_pins_source_date_epoch():
    body = (ROOT / "packaging" / "container" / "kpclientd-build.sh").read_text()
    assert "dpkg --print-architecture" in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "kpclientd_0.0.1_amd64.deb" not in body
