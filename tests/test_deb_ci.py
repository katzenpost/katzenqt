import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deb.yml"
CI = ROOT / "packaging" / "debian" / "ci.sh"
TARGETS = ROOT / "packaging" / "debian" / "targets.mk"
IMAGE_SH = ROOT / "packaging" / "container" / "deb-image.sh"


def distros() -> list[str]:
    found = re.search(
        r"^DEB_DISTROS \?= (.+)$", TARGETS.read_text(), re.MULTILINE
    )
    assert found
    return found.group(1).split()


def test_every_distro_is_built_through_the_make_target() -> None:
    wf = WORKFLOW.read_text()
    assert distros() == ["debian-13", "debian-forky", "ubuntu-26.04"]
    for distro in distros():
        assert distro in wf, distro
    assert "make deb-ci" in wf
    assert "packaging/debian/build.sh" in CI.read_text()
    assert "dpkg-buildpackage" not in wf
    assert "namenlos" not in wf.lower()
    assert "pip install" not in wf


MK_BUILD_DEPS = "mk-build-deps -ir"


def test_build_deps_come_from_debian_control() -> None:
    ci = CI.read_text()
    assert MK_BUILD_DEPS in ci
    assert "debian/control" in ci
    assert not (ROOT / "packaging" / "debian" / "deps.txt").exists()
    ci_image = ROOT / "packaging" / "container" / "Containerfile.ci"
    image = ci_image.read_text()
    assert "COPY debian/control" in image
    assert MK_BUILD_DEPS in image


def test_the_image_is_built_once_from_an_official_base() -> None:
    wf = WORKFLOW.read_text()
    image = (
        ROOT / "packaging" / "container" / "Containerfile.ci"
    ).read_text()
    local = IMAGE_SH.read_text()
    assert "make deb-image-refs" in wf
    assert "make deb-image-push DISTRO=" in wf
    assert "ghcr.io/katzenpost/katzenqt/deb-build" in local
    assert "manifest inspect" in local
    assert "exists; not rebuilding" in local
    assert "base=docker.io/" in local
    assert "ARG BASE=docker.io/debian:13" in image
    assert "FROM ${BASE}" in image


def test_ci_pulls_the_published_image_rather_than_installing_deps() -> None:
    wf = WORKFLOW.read_text()
    for key in ("ref_debian_13", "ref_debian_forky", "ref_ubuntu_26_04"):
        assert key in wf, key
    assert "container:" in wf
    assert "apt-get" not in wf


def test_go_is_the_pinned_toolchain_not_the_distro_one() -> None:
    image = (ROOT / ".ci" / "Dockerfile").read_text()
    ci = CI.read_text()
    assert "golang-go" not in ci
    assert "https://go.dev/dl/go" in ci
    assert "sha256sum -c -" in ci
    assert "devscripts" in image
    assert "equivs" in image


def test_the_closure_toolchain_is_declared() -> None:
    ci = CI.read_text()
    for pkg in ("cargo", "rustc", "cmake", "python3-pip", "unzip", "git"):
        assert pkg in ci, pkg
