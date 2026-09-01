import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "katzenqt_flathub_release", ROOT / "packaging/flatpak/release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_tag_details_requires_annotated_main_tag(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        values = {
            ("git", "cat-file", "-t", "v0.0.1"): "tag",
            ("git", "rev-parse", "v0.0.1^{commit}"): "abc",
            ("git", "rev-parse", "HEAD"): "abc",
            ("git", "status", "--porcelain"): "",
            ("git", "rev-parse", "FETCH_HEAD"): "def",
            (
                "git",
                "show",
                "v0.0.1:pyproject.toml",
            ): '[project]\nversion = "0.0.1"',
            (
                "git",
                "show",
                "v0.0.1:packaging/flatpak/"
                "network.katzenpost.katzenqt.metainfo.xml",
            ): (
                '<component><releases><release version="0.0.1" '
                'date="2026-08-28"/></releases></component>'
            ),
            ("git", "log", "-1", "--format=%ct", "v0.0.1"): "1787875200",
            (
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/tags/v0.0.1",
            ): "abc",
        }
        return values.get(args, "")

    monkeypatch.setattr(release, "run", fake_run)
    assert release.tag_details("v0.0.1", True) == (
        "abc",
        "0.0.1",
        "2026-08-28",
    )
    assert ("git", "fetch", "origin", "main") in calls
    assert ("git", "merge-base", "--is-ancestor", "abc", "def") in calls


def test_tag_details_rejects_lightweight_tag(monkeypatch):
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: "commit")
    with pytest.raises(ValueError, match="annotated"):
        release.tag_details("v0.0.1")


def test_tag_details_requires_tag_checkout(monkeypatch):
    values = {
        ("git", "cat-file", "-t", "0.0.1"): "tag",
        ("git", "rev-parse", "0.0.1^{commit}"): "tagged",
        ("git", "rev-parse", "HEAD"): "other",
    }
    monkeypatch.setattr(
        release, "run", lambda *args, **kwargs: values.get(args, "")
    )
    with pytest.raises(ValueError, match="HEAD"):
        release.tag_details("0.0.1")


def test_tag_details_accepts_repository_tag_style(monkeypatch):
    monkeypatch.setattr(
        release,
        "run",
        lambda *args, **kwargs: {
            ("git", "cat-file", "-t", "0.0.1"): "tag",
            ("git", "rev-parse", "0.0.1^{commit}"): "abc",
            ("git", "rev-parse", "HEAD"): "abc",
            ("git", "status", "--porcelain"): "",
            (
                "git",
                "show",
                "0.0.1:pyproject.toml",
            ): '[project]\nversion = "0.0.1"',
            (
                "git",
                "show",
                "0.0.1:packaging/flatpak/"
                "network.katzenpost.katzenqt.metainfo.xml",
            ): (
                '<component><releases><release version="0.0.1" '
                'date="2026-08-28"/></releases></component>'
            ),
            ("git", "log", "-1", "--format=%ct", "0.0.1"): "1787875200",
        }.get(args, ""),
    )
    assert release.tag_details("0.0.1")[1] == "0.0.1"


def test_stage_contains_only_tagged_application_source(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release, "tag_details", lambda *args: ("abc", "0.0.1", "2026-08-28")
    )
    monkeypatch.setattr(
        release,
        "archive",
        lambda tag: ("https://example/v0.0.1.tar.gz", "a" * 64),
    )
    files = {
        "packaging/flatpak/LICENSE": b"license",
        "packaging/flatpak/README.md": b"readme",
        "packaging/flatpak/flathub.json": b'{"skip-arches":["aarch64"]}',
        "packaging/flatpak/pyside6-sources.json": b"[]",
        "packaging/flatpak/python3-deps.json": b"{}",
        f"packaging/flatpak/{release.APP_ID}.metainfo.xml": (
            b"https://raw.githubusercontent.com/katzenpost/katzenqt/main/"
            b"packaging/flatpak/screenshots/katzenqt.png"
        ),
        f"packaging/{release.APP_ID}.desktop": b"desktop",
        "packaging/flatpak/screenshots/katzenqt.png": b"png",
        f"packaging/flatpak/{release.APP_ID}.yaml": (
            release.PACKAGE / f"{release.APP_ID}.yaml"
        ).read_bytes(),
    }
    monkeypatch.setattr(release, "tagged_file", lambda tag, path: files[path])
    destination = release.stage("v0.0.1", tmp_path / "dist")
    manifest = (destination / f"{release.APP_ID}.yaml").read_text()
    assert "https://example/v0.0.1.tar.gz" in manifest
    assert f"sha256: {'a' * 64}" in manifest
    assert "../../" not in manifest
    assert f"path: {release.APP_ID}.desktop" in manifest
    assert (
        "/v0.0.1/packaging/flatpak/screenshots/"
        in (destination / f"{release.APP_ID}.metainfo.xml").read_text()
    )

    second = release.stage("v0.0.1", tmp_path / "second")
    first_files = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files


def test_stage_uses_tagged_packaging_not_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release, "tag_details", lambda *args: ("abc", "0.0.1", "2026-08-28")
    )
    monkeypatch.setattr(
        release,
        "archive",
        lambda tag: ("https://example/0.0.1.tar.gz", "a" * 64),
    )
    tagged = b"app-id: network.katzenpost.katzenqt\nmodules: []\n"
    monkeypatch.setattr(
        release,
        "tagged_file",
        lambda tag, path: tagged if path.endswith(".yaml") else b"tagged",
    )
    destination = release.stage("0.0.1", tmp_path / "dist")
    assert (
        (destination / f"{release.APP_ID}.yaml")
        .read_bytes()
        .startswith(b"app-id:")
    )


def test_submit_opens_pr_without_merging(tmp_path, monkeypatch):
    calls = []
    destination = tmp_path / "dist"

    def fake_stage(*args):
        destination.mkdir(exist_ok=True)
        (destination / "manifest").write_text("data")

    def fake_run(*args, cwd=release.ROOT, capture=True):
        calls.append(args)
        if args[:4] == ("gh", "repo", "fork", "--clone"):
            Path(args[-1]).mkdir()
        return ""

    monkeypatch.setattr(release, "tag_details", lambda *args: None)
    monkeypatch.setattr(release, "stage", fake_stage)
    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1),
    )
    release.submit("v0.0.1", destination)
    assert any(call[:3] == ("gh", "pr", "create") for call in calls)
    assert not any(call[:3] == ("gh", "pr", "merge") for call in calls)
