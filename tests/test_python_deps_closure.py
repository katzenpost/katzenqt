import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "packaging" / "container" / "pydeps-build.sh"
PINS = ROOT / "packaging" / "debian" / "targets.mk"
CI = ROOT / "packaging" / "debian" / "ci.sh"


def _group(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    assert match, pattern
    return match.group(1)


def test_pins_are_explicit() -> None:
    makefile = PINS.read_text()
    assert re.search(
        r"^PYCRDT_URL := https://github.com/", makefile, re.MULTILINE
    )
    assert re.search(r"^PYCRDT_REV := [0-9a-f]{40}$", makefile, re.MULTILINE)
    assert re.search(
        r"^THINCLIENT_URL := https://github.com/", makefile, re.MULTILINE
    )
    assert re.search(
        r"^THINCLIENT_REV := [0-9a-f]{40}$", makefile, re.MULTILINE
    )
    assert re.search(
        r"^RUSTIC_AUDIO_URL := https://github.com/", makefile, re.MULTILINE
    )
    assert re.search(
        r"^RUSTIC_AUDIO_REV := [0-9a-f]{40}$", makefile, re.MULTILINE
    )


def test_closure_builds_from_source_not_prebuilt_binaries() -> None:
    assert os.access(SCRIPT, os.X_OK)
    body = SCRIPT.read_text()
    assert "$PYCRDT_URL" in body and "$PYCRDT_REV" in body
    assert '"$PIP" wheel' in body
    assert "python3-pycrdt" in body
    assert "python3-rustic-audio-tool" in body
    assert "pprintpp" not in body
    assert "katzenpost_thinclient" not in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "$RUSTIC_AUDIO_URL" in body and "$RUSTIC_AUDIO_REV" in body


def test_vendored_debs_declare_their_debian_runtime_deps() -> None:
    body = SCRIPT.read_text()
    for dep in ("python3-anyio", "libasound2"):
        assert dep in body


def test_the_thin_client_comes_from_its_own_repo_not_pypi() -> None:
    ci = CI.read_text()
    assert '"$THINCLIENT_URL" "$THINCLIENT_REV"' in ci
    assert "packaging/container/dep-deb.sh" in ci
    assert "pprintpp" not in ci
    body = SCRIPT.read_text()
    assert "pprintpp" not in body


def test_closure_container_has_rust_toolchain() -> None:
    dockerfile = (
        ROOT / "packaging" / "container" / "Containerfile.python-debs"
    ).read_text()
    assert "cargo" in dockerfile
    assert "python3-dev" in dockerfile


def test_make_target_and_all_wire_the_closure() -> None:
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    recipe = _group(r"^container-pydeps:.*\n((?:\t.*\n)+)", mk)
    assert "../container/build-python-debs.sh" in recipe
    all_recipe = _group(r"^container-all:.*\n((?:\t.*\n)+)", mk)
    assert "build-python-debs.sh" in all_recipe


def test_ci_builds_and_installs_the_closure() -> None:
    wf = (ROOT / ".github" / "workflows" / "deb.yml").read_text()
    ci = CI.read_text()
    assert "make deb-ci" in wf
    assert "packaging/container/pydeps-build.sh" in ci
    assert '"$debs"/python3-katzenpost-thinclient_*.deb' in ci
    assert "apt install" in ci
    assert "pip install" not in ci


def test_closure_is_built_reproducibly() -> None:
    body = SCRIPT.read_text()
    assert "SOURCE_DATE_EPOCH" in body
    assert "PIP_VER" in body
    assert "MATURIN_VER" in body


def test_the_top_level_makefile_only_includes_the_packaging_targets() -> None:
    body = (ROOT / "Makefile").read_text()
    assert "-include packaging/*/targets.mk" in body
    assert "PYCRDT_URL" not in body
    assert re.search(r"^deb:", body, re.MULTILINE) is None
