import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "packaging" / "container" / "pydeps-build.sh"
PINS = ROOT / "packaging" / "debian" / "targets.mk"
CI = ROOT / "packaging" / "debian" / "ci.sh"
FLATPAK = ROOT / "packaging" / "flatpak" / "python3-deps.json"
PROJECT = ROOT / "pyproject.toml"


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
    assert re.search(
        r"^HPQC_URL := https://github.com/", makefile, re.MULTILINE
    )
    assert re.search(r"^HPQC_REV := [0-9a-f]{40}$", makefile, re.MULTILINE)
    assert re.search(
        r"^HIGHCTIDH_URL := https://codeberg.org/", makefile, re.MULTILINE
    )
    assert re.search(
        r"^HIGHCTIDH_REV := [0-9a-f]{40}$", makefile, re.MULTILINE
    )


def test_every_place_that_pins_hpqc_pins_the_same_commit() -> None:
    rev = _group(r"^HPQC_REV := ([0-9a-f]{40})$", PINS.read_text())
    assert _group(
        r'hpqc = \{ git = "[^"]+", rev = "([0-9a-f]{40})"',
        PROJECT.read_text(),
    ) == rev
    flatpak = json.loads(FLATPAK.read_text())
    module = next(
        m for m in flatpak["modules"] if m.get("name") == "python3-hpqc"
    )
    commits = [
        src["commit"] for src in module["sources"] if "commit" in src
    ]
    assert commits == [rev]


def test_every_arch_gets_the_same_set_of_wheels() -> None:
    flatpak = json.loads(FLATPAK.read_text())
    for module in flatpak["modules"]:
        per_arch: "dict[str, set[str]]" = {}
        for src in module.get("sources", []):
            name = src.get("url", "").rsplit("/", 1)[-1]
            if not name.endswith(".whl"):
                continue
            for arch in src.get("only-arches", []):
                per_arch.setdefault(arch, set()).add(name.split("-")[0])
        if len(per_arch) < 2:
            continue
        assert len(set(map(frozenset, per_arch.values()))) == 1, (
            module["name"], {a: sorted(n) for a, n in per_arch.items()},
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


def test_one_script_builds_every_dependency() -> None:
    """A clone and one target has to yield the whole closure, and hpqc
    and highctidh are built by their own targets, not repacked here."""
    body = SCRIPT.read_text()
    for name in ("pycrdt", "rustic-audio-tool", "HPQC_URL",
                 "HIGHCTIDH_URL"):
        assert name in body
    assert '"$dep" "$out" "$HPQC_URL" "$HPQC_REV"' in body
    assert '"$HIGHCTIDH_REV" deb' in body
    dep = (ROOT / "packaging" / "container" / "dep-deb.sh").read_text()
    assert "target=${4:-deb-build}" in dep
    assert '"$target"' in dep
    collect = (
        ROOT / "packaging" / "container" / "build-python-debs.sh"
    ).read_text()
    for name in ("python3-hpqc_", "python3-highctidh_"):
        assert name in collect
    ci = CI.read_text()
    assert "packaging/container/pydeps-build.sh" in ci
    assert "dep-deb.sh \"$debs\" \"$HPQC_URL\"" not in ci


def test_the_installed_package_imports_what_the_closure_builds() -> None:
    body = (ROOT / "packaging" / "debian" / "test.sh").read_text()
    assert "import hpqc.bacap" in body
    assert "from hpqc.nike.ctidh1024 import CTIDH1024" in body


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
    for tool in ("cargo", "python3-dev", "dh-golang", "golang-any",
                 "python3-stdeb", "flit"):
        assert tool in dockerfile


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
    assert '"$debs"/python3-highctidh_*.deb' in ci
    assert '"$debs"/python3-hpqc_*.deb' in ci
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


def test_the_debian_audio_pin_matches_the_application_lock() -> None:
    """The Debian build vendors the audio crate from its own pin, so a bump
    in uv.lock that misses this file ships a different encoder."""
    makefile = (ROOT / "packaging" / "debian" / "targets.mk").read_text(
        encoding="ascii",
    )
    pinned = re.search(
        r"^RUSTIC_AUDIO_REV := ([0-9a-f]{40})$", makefile, re.MULTILINE,
    )
    assert pinned is not None
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert pinned.group(1) in lock
