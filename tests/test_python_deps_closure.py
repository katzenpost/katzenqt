import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "packaging" / "container" / "pydeps-build.sh"


def test_pins_are_explicit():
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^PYCRDT_URL := https://github.com/", makefile, re.MULTILINE)
    assert re.search(r"^PYCRDT_REV := [0-9a-f]{40}$", makefile, re.MULTILINE)
    assert re.search(r"^THINCLIENT_VER := ", makefile, re.MULTILINE)
    assert re.search(r"^PPRINTPP_VER := ", makefile, re.MULTILINE)
    assert re.search(r"^RUSTIC_AUDIO_URL := https://github.com/", makefile, re.MULTILINE)
    assert re.search(r"^RUSTIC_AUDIO_REV := [0-9a-f]{40}$", makefile, re.MULTILINE)


def test_closure_builds_from_source_not_prebuilt_binaries():
    assert os.access(SCRIPT, os.X_OK)
    body = SCRIPT.read_text()
    assert "$PYCRDT_URL" in body and "$PYCRDT_REV" in body
    assert '"$PIP" wheel' in body
    assert "--no-binary :all:" in body
    assert "python3-pycrdt" in body
    assert "python3-katzenpost-thinclient" in body
    assert "python3-rustic-audio-tool" in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "$RUSTIC_AUDIO_URL" in body and "$RUSTIC_AUDIO_REV" in body


def test_vendored_debs_declare_their_debian_runtime_deps():
    body = SCRIPT.read_text()
    for dep in (
        "python3-anyio",
        "python3-cbor2",
        "python3-coloredlogs",
        "python3-pprintpp",
        "python3-toml",
    ):
        assert dep in body


def test_pprintpp_ships_no_compiled_object():
    body = SCRIPT.read_text()
    assert "python3-pprintpp" in body
    assert "--only-binary :all:" in body
    assert "refusing" in body


def test_closure_container_has_rust_toolchain():
    dockerfile = (ROOT / "packaging" / "container" / "Containerfile.python-debs").read_text()
    assert "cargo" in dockerfile
    assert "python3-dev" in dockerfile


def test_make_target_and_all_wire_the_closure():
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    recipe = re.search(
        r"^container-pydeps:.*\n((?:\t.*\n)+)", mk, re.MULTILINE
    ).group(1)
    assert "../container/build-python-debs.sh" in recipe
    all_recipe = re.search(
        r"^container-all:.*\n((?:\t.*\n)+)", mk, re.MULTILINE
    ).group(1)
    assert "build-python-debs.sh" in all_recipe


def test_ci_builds_and_installs_the_closure():
    wf = (ROOT / ".github" / "workflows" / "deb-multi-os.yml").read_text()
    assert "packaging/container/pydeps-build.sh" in wf
    assert "apt install" in wf
    assert "pip install" not in wf


def test_closure_is_built_reproducibly():
    body = SCRIPT.read_text()
    assert "SOURCE_DATE_EPOCH" in body
    assert "PIP_VER" in body
    assert "MATURIN_VER" in body
