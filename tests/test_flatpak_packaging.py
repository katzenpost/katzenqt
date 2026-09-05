import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
FLATPAK = ROOT / "packaging" / "flatpak"
MANIFEST = FLATPAK / "network.katzenpost.katzenqt.yaml"
MAKEFILE = (ROOT / "packaging" / "flatpak" / "targets.mk").read_text()


def target_body(name: str) -> "list[str]":
    match = re.search(
        rf"^{re.escape(name)}:.*\n((?:\t.*\n?)+)", MAKEFILE, re.MULTILINE
    )
    assert match, f"no target {name}"
    return [line for line in match.group(1).splitlines() if line.strip()]


def test_manifest_builds_from_a_clean_git_archive() -> None:
    data = MANIFEST.read_text()
    assert "path: katzenqt-src.tar.gz" in data
    assert "strip-components: 0" in data
    assert "github.com/katzenpost/katzenqt/archive" not in data
    assert "../../src/katzenqt" not in data


def test_manifest_allows_no_network_and_no_dbus() -> None:
    data = MANIFEST.read_text()
    assert "--share=network" not in data
    assert "--talk-name" not in data
    assert "--own-name" not in data
    assert "--socket=fallback-x11" in data
    assert "--filesystem=xdg-run/katzenpost:ro" in data
    assert "--filesystem=home" not in data
    assert "--filesystem=host" not in data


def test_manifest_does_not_bundle_kpclientd() -> None:
    data = MANIFEST.read_text()
    assert "go build" not in data
    assert "/app/bin/kpclientd" not in data


def test_metadata_ids_match() -> None:
    app_id = "network.katzenpost.katzenqt"
    manifest = MANIFEST.read_text()
    desktop = (ROOT / "packaging" / f"{app_id}.desktop").read_text()
    metainfo = (FLATPAK / f"{app_id}.metainfo.xml").read_text()
    assert f"app-id: {app_id}" in manifest
    assert f"<id>{app_id}</id>" in metainfo
    assert '<developer id="network.katzenpost">' in metainfo
    assert "<name>Katzenpost</name>" in metainfo
    assert "Exec=katzenqt" in desktop
    assert "Experimental" in metainfo
    for warning in (
        "security",
        "anonymity",
        "privacy",
        "reliability",
        "message delivery",
        "data retention",
    ):
        assert warning in metainfo


def test_shared_desktop_is_reused_not_duplicated() -> None:
    assert not (FLATPAK / "network.katzenpost.katzenqt.desktop").exists()
    assert (
        ROOT / "packaging" / "network.katzenpost.katzenqt.desktop"
    ).is_file()
    assert (
        "path: ../network.katzenpost.katzenqt.desktop" in MANIFEST.read_text()
    )


def test_scripts_are_executable_posix_sh() -> None:
    for name in (
        "build.sh",
        "run.sh",
        "install.sh",
        "system-deps.sh",
        "lint.sh",
        "reproducible.sh",
    ):
        script = FLATPAK / name
        assert os.access(script, os.X_OK), name
        assert script.read_text().startswith("#!/bin/sh"), name


def test_build_script_builds_and_folds_the_checks() -> None:
    body = (FLATPAK / "build.sh").read_text()
    assert 'git -C "$root" archive' in body
    assert "flatpak-builder" in body
    assert "make flatpak-system-deps" in body
    assert "lint.sh" in body
    assert "reproducible.sh" in body
    assert "appstreamcli validate" in body
    assert "desktop-file-validate" in body
    assert "check.py" in body


def test_no_dbus_anywhere_in_flatpak_dir() -> None:
    for path in FLATPAK.rglob("*"):
        if path.is_file() and path.suffix not in (".png",):
            text = path.read_text(errors="ignore").lower()
            assert "dbus" not in text, path
            assert "talk-name" not in text, path


def test_flatpak_targets_are_thin_wrappers() -> None:
    wrappers = {
        "flatpak-build": "packaging/flatpak/build.sh",
        "flatpak-install": "packaging/flatpak/install.sh",
        "flatpak-run": "packaging/flatpak/run.sh",
        "flatpak-system-deps": "packaging/flatpak/system-deps.sh",
    }
    for target, script in wrappers.items():
        body = target_body(target)
        assert len(body) == 1, target
        assert body[0].strip() == f"@{script}", target


def test_no_per_check_make_targets() -> None:
    for gone in ("^lint:", "^reproducible:", "^validate:", "^flatpak:"):
        assert not re.search(gone, MAKEFILE, re.MULTILINE), gone


def test_build_runs_inside_podman() -> None:
    body = (FLATPAK / "build.sh").read_text()
    assert "command -v podman" in body
    assert "container/build.sh" in body


def test_container_driver_declares_the_privileged_flags() -> None:
    driver = FLATPAK / "container" / "build.sh"
    assert os.access(driver, os.X_OK)
    body = driver.read_text()
    assert body.startswith("#!/bin/sh")
    assert "PODMAN:-podman" in body
    assert '"$PODMAN" build' in body
    assert '"$PODMAN" run' in body
    assert "--privileged" in body
    assert "--device /dev/fuse" in body


def test_container_image_carries_the_toolchain() -> None:
    image = (FLATPAK / "container" / "Containerfile").read_text()
    assert "flatpak-builder" in image
    assert "flathub" in image
    assert "org.kde.Platform//6.9" in image
    assert "org.kde.Sdk//6.9" in image
    assert "org.flatpak.Builder" in image
    assert "apt-get" not in image


def test_gitignore_covers_the_build_outputs() -> None:
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    ignore += [
        f"packaging/flatpak/{line}"
        for line in (FLATPAK / ".gitignore").read_text().splitlines()
    ]
    for entry in (
        ".flatpak-build/",
        ".flatpak-builder/",
        ".flatpak-export/",
        ".flatpak-repo/",
        "packaging/flatpak/katzenqt-src.tar.gz",
    ):
        assert entry in ignore, entry


def test_screenshot_is_real_and_pinned() -> None:
    screenshot = FLATPAK / "screenshots" / "katzenqt.png"
    metainfo = (
        FLATPAK / "network.katzenpost.katzenqt.metainfo.xml"
    ).read_text()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert screenshot.stat().st_size > 40_000
    assert "/main/packaging/flatpak/screenshots/katzenqt.png" in metainfo


def test_build_mirrors_the_screenshot() -> None:
    body = (FLATPAK / "build.sh").read_text()
    assert "mirror-screenshot.py" in body
    assert " catalog " in body
    assert " repo " in body


def test_flatpak_test_target_is_thin() -> None:
    body = target_body("flatpak-test")
    assert len(body) == 1
    assert body[0].strip() == "@packaging/flatpak/test.sh"


def test_docker_test_script() -> None:
    script = FLATPAK / "test.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    assert "trap cleanup EXIT INT TERM" in body
    assert "managed test mixnet is still reachable" in body
    assert "running.stamp" in body
    assert body.index("trap cleanup") < body.index("start wait")
    assert "- imports < " in body
    assert "- permissions < " in body
    assert "test ! -e /app/bin/kpclientd" in body


def test_integration_wrapper_dies_with_parent() -> None:
    wrapper = (FLATPAK / "integration-python").read_text()
    assert "flatpak run --die-with-parent" in wrapper


def test_flatpak_release_target_calls_the_script() -> None:
    body = target_body("flatpak-release")
    assert len(body) == 1
    assert "packaging/flatpak/release.sh" in body[0]


def test_release_script_runs_the_release_stages() -> None:
    script = FLATPAK / "release.sh"
    assert os.access(script, os.X_OK)
    body = script.read_text()
    for stage in ("validate", "dist", "check", "submit"):
        assert f'release.py" {stage}' in body


def test_flathub_skips_unsupported_arch() -> None:
    flathub = FLATPAK / "flathub.json"
    assert json.loads(flathub.read_text()) == {"skip-arches": ["aarch64"]}


def test_flatpak_pins_match_the_application_lock() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    thin = next(
        p for p in lock["package"] if p["name"] == "katzenpost-thinclient"
    )
    modules = json.loads((FLATPAK / "python3-deps.json").read_text())[
        "modules"
    ]
    module = next(
        p for p in modules if p["name"] == "python3-katzenpost_thinclient"
    )
    wheel = thin["wheels"][0]
    assert any(
        source.get("url") == wheel["url"]
        and source["sha256"] == wheel["hash"].removeprefix("sha256:")
        for source in module["sources"]
    )
    audio = json.loads((FLATPAK / "rustic-audio-tool.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        audio["sources"][0]["commit"]
        == (project["tool"]["uv"]["sources"]["rustic-audio-tool"]["rev"])
    )
    assert "--socket=pulseaudio" in MANIFEST.read_text()


def test_help_lists_every_flatpak_target() -> None:
    names = re.findall(r"^(flatpak-[\w-]+):", MAKEFILE, re.MULTILINE)
    out = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert names
    assert [n for n in names if f"make {n} " in out] == names
