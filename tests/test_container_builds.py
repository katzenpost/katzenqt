import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONTAINER = ROOT / "packaging" / "container"
DEBIAN_MK = ROOT / "packaging" / "debian" / "Makefile"


def test_containerfile_is_parameterized_by_base():
    body = (CONTAINER / "Containerfile").read_text()
    assert "ARG BASE=" in body
    assert "FROM ${BASE}" in body
    assert "ARG BUILD_DEPS=" in body


def test_overrides_exist_for_each_distro():
    makefile = DEBIAN_MK.read_text()
    distros = re.search(r"^DISTROS \?= (.*)$", makefile, re.MULTILINE).group(1).split()
    assert "ubuntu-26.04" in distros
    assert "debian-13" in distros
    for distro in distros:
        env = (CONTAINER / "overrides" / f"{distro}.env").read_text()
        assert "BASE=" in env
        assert "BUILD_DEPS=" in env


def test_build_driver_runs_the_deb_build_in_the_container():
    script = CONTAINER / "build.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "$PODMAN" in body or "podman" in body
    assert "packaging/container/Containerfile" in body
    assert "packaging/debian/build.sh" in body


def test_make_targets_drive_the_container():
    makefile = DEBIAN_MK.read_text()
    recipe = re.search(
        r"^container:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE
    ).group(1)
    assert "../container/build.sh" in recipe
    assert re.search(r"^container-all:", makefile, re.MULTILINE)
    assert re.search(r"^container-clean:", makefile, re.MULTILINE)
    assert os.access(ROOT / "packaging" / "debian" / "container-clean.sh", os.X_OK)


def test_makefile_is_podman_first_with_no_host_targets():
    makefile = DEBIAN_MK.read_text()
    targets = re.findall(r"^([A-Za-z0-9_-]+):", makefile, re.MULTILINE)
    assert targets[0] == "container"
    for gone in ("build", "deps", "all", "install"):
        assert not re.search(rf"^{gone}:", makefile, re.MULTILINE)
    assert "install-build-deps" not in makefile
    assert "dpkg-buildpackage" not in makefile
    assert "apt install" not in makefile
