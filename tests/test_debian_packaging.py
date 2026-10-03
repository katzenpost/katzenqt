import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"
CI = ROOT / "packaging" / "debian" / "ci.sh"
TEST_SH = ROOT / "packaging" / "debian" / "test.sh"


def test_control_is_project_authored_with_apt_deps() -> None:
    control = (DEBIAN / "control").read_text()
    assert "Architecture: all" in control
    assert "Maintainer: Katzenpost" in control
    assert "jacob" not in control.lower()
    assert "python3-pyside6" in control
    assert "python3-nacl" in control
    assert "pip install" not in control
    assert "venv" not in control


def test_changelog_is_native_versioned_and_project_authored() -> None:
    changelog = (DEBIAN / "changelog").read_text()
    assert changelog.startswith("katzenqt (0.0.1) unstable")
    assert "Katzenpost" in changelog


def test_copyright_declares_project_and_file_licenses() -> None:
    copyright_text = (DEBIAN / "copyright").read_text()
    assert "License: public-domain" in copyright_text
    assert "License: AGPL-3" in copyright_text
    assert "Katzenpost" in copyright_text


def test_rules_pins_source_date_epoch_and_uses_pybuild() -> None:
    rules = (DEBIAN / "rules").read_text()
    assert "--buildsystem=pybuild" in rules
    assert "--with python3" in rules
    assert (
        "SOURCE_DATE_EPOCH ?= $(shell dpkg-parsechangelog -STimestamp)"
        in rules
    )


def test_native_source_format() -> None:
    assert (
        DEBIAN / "source" / "format"
    ).read_text().strip() == "3.0 (native)"


def test_unpackaged_deps_are_mapped() -> None:
    overrides = (DEBIAN / "py3dist-overrides").read_text()
    assert "pycrdt python3-pycrdt" in overrides
    assert "katzenpost-thinclient python3-katzenpost-thinclient" in overrides


def test_desktop_and_icon_are_installed() -> None:
    install = (DEBIAN / "katzenqt.install").read_text()
    assert (
        "network.katzenpost.katzenqt.desktop usr/share/applications"
        in install
    )
    assert "packaging/*.svg usr/share/icons/hicolor/scalable/apps" in install
    icons = sorted(p.name for p in (ROOT / "packaging").glob("*.svg"))
    assert icons == ["network.katzenpost.katzenqt.svg"]
    desktop = ROOT / "packaging" / "network.katzenpost.katzenqt.desktop"
    assert "Icon=network.katzenpost.katzenqt" in desktop.read_text()


def test_build_script_ships_deb_into_dist() -> None:
    script = ROOT / "packaging" / "debian" / "build.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "dpkg-buildpackage" in body
    assert "dist/" in body


def test_make_deb_delegates_to_the_debian_dir() -> None:
    targets = (ROOT / "packaging" / "debian" / "targets.mk").read_text()
    assert re.search(
        r"^deb:\n\t@\$\(MAKE\) -C packaging/debian$",
        targets,
        re.MULTILINE,
    )


def test_no_generated_debhelper_artifacts_committed() -> None:
    assert not list(DEBIAN.glob("*.debhelper"))


def test_ci_invokes_the_packaging_scripts() -> None:
    ci = CI.read_text()
    assert "packaging/debian/build.sh" in ci
    assert "packaging/container/pydeps-build.sh" in ci
    assert "packaging/container/kpclientd-build.sh" in ci
    assert "python3-rustic-audio-tool" in ci
    assert ".wants/kpclientd.service" in TEST_SH.read_text()
    assert "pip install" not in ci


def test_help_lists_every_debian_target() -> None:
    targets = (ROOT / "packaging" / "debian" / "targets.mk").read_text()
    names = re.findall(r"^(deb[\w-]*):", targets, re.MULTILINE)
    out = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert names
    assert [n for n in names if f"make {n} " in out] == names
