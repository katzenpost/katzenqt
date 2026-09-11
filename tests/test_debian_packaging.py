import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"


def test_control_is_project_authored_with_apt_deps():
    control = (DEBIAN / "control").read_text()
    assert "Architecture: all" in control
    assert "Maintainer: Katzenpost" in control
    assert "jacob" not in control.lower()
    assert "python3-pyside6" in control
    assert "python3-nacl" in control
    assert "pip install" not in control
    assert "venv" not in control


def test_changelog_is_native_versioned_and_project_authored():
    changelog = (DEBIAN / "changelog").read_text()
    assert changelog.startswith("katzenqt (0.0.1) unstable")
    assert "Katzenpost" in changelog


def test_copyright_declares_project_and_file_licenses():
    copyright_text = (DEBIAN / "copyright").read_text()
    assert "License: public-domain" in copyright_text
    assert "License: AGPL-3" in copyright_text
    assert "Katzenpost" in copyright_text


def test_rules_pins_source_date_epoch_and_uses_pybuild():
    rules = (DEBIAN / "rules").read_text()
    assert "--buildsystem=pybuild" in rules
    assert "--with python3" in rules
    assert "SOURCE_DATE_EPOCH ?= $(shell dpkg-parsechangelog -STimestamp)" in rules


def test_native_source_format():
    assert (DEBIAN / "source" / "format").read_text().strip() == "3.0 (native)"


def test_unpackaged_deps_are_mapped():
    overrides = (DEBIAN / "py3dist-overrides").read_text()
    assert "pycrdt python3-pycrdt" in overrides
    assert "katzenpost-thinclient python3-katzenpost-thinclient" in overrides


def test_desktop_and_icon_are_installed():
    install = (DEBIAN / "katzenqt.install").read_text()
    assert "network.katzenpost.katzenqt.desktop usr/share/applications" in install
    assert "resources/echomix_256.png usr/share/icons/hicolor/256x256/apps" in install
    assert (ROOT / "resources" / "echomix_256.png").is_file()
    desktop = ROOT / "packaging" / "network.katzenpost.katzenqt.desktop"
    assert "Icon=echomix_256" in desktop.read_text()


def test_build_script_ships_deb_into_dist():
    script = ROOT / "packaging" / "debian" / "build.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "dpkg-buildpackage" in body
    assert "dist/" in body


def test_make_deb_delegates_to_the_debian_dir():
    root_mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^deb:\n\t@\$\(MAKE\) -C packaging/debian$", root_mk, re.MULTILINE)


def test_no_generated_debhelper_artifacts_committed():
    assert not list(DEBIAN.glob("*.debhelper"))


def test_multi_os_ci_covers_both_targets_and_invokes_the_scripts():
    wf = (ROOT / ".github" / "workflows" / "deb-multi-os.yml").read_text()
    assert "debian:13" in wf
    assert "ubuntu:26.04" in wf
    assert "dpkg-buildpackage" in wf
    assert "packaging/container/pydeps-build.sh" in wf
    assert "packaging/container/kpclientd-build.sh" in wf
    assert "python3-rustic-audio-tool" in wf
    assert ".wants/kpclientd.service" in wf
    assert "namenlos" not in wf.lower()
    assert "pip install" not in wf
