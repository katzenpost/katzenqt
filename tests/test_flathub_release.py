import importlib.util
import io
import subprocess
import tarfile
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
        "packaging/flatpak/rustic-audio-tool.json": b"audio-module",
        "packaging/flatpak/rustic-audio-sources.json": b"audio-sources",
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
    assert (destination / "rustic-audio-tool.json").read_bytes() == b"audio-module"
    assert (destination / "rustic-audio-sources.json").read_bytes() == b"audio-sources"
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
    tagged = (
        b"app-id: network.katzenpost.katzenqt\n"
        b"      - type: archive\n        path: katzenqt-src.tar.gz\n"
        b"path: ../network.katzenpost.katzenqt.desktop\n"
    )
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
    destination.mkdir()
    (destination / ".checked-tag").write_text("v0.0.1\n")

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


def test_submit_requires_a_matching_checked_tag(tmp_path, monkeypatch):
    destination = tmp_path / "dist"
    destination.mkdir()
    monkeypatch.setattr(release, "tag_details", lambda *args: None)
    monkeypatch.setattr(
        release, "stage", lambda *args: pytest.fail("staged without a check")
    )
    with pytest.raises(ValueError, match="check before submit"):
        release.submit("v0.0.1", destination)
    (destination / ".checked-tag").write_text("v9.9.9\n")
    with pytest.raises(ValueError, match="check before submit"):
        release.submit("v0.0.1", destination)


def test_stage_fails_when_manifest_formatting_drifts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release, "tag_details", lambda *args: ("abc", "0.0.1", "2026-08-28")
    )
    monkeypatch.setattr(
        release, "archive", lambda tag: ("https://example/0.0.1.tar.gz", "a" * 64)
    )
    monkeypatch.setattr(release, "tagged_file", lambda tag, path: b"app-id: drift\n")
    with pytest.raises(ValueError, match="formatting drifted"):
        release.stage("0.0.1", tmp_path / "dist")


def _tar_bytes(files, prefix, mode):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_verify_archive_accepts_matching_tarballs(monkeypatch):
    files = {"packaging/x": b"one", "src/app.py": b"two"}
    fetched = _tar_bytes(files, "katzenqt-0.0.1/", "w:gz")
    local = _tar_bytes(files, "", "w")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=local),
    )
    release.verify_archive("v0.0.1", fetched)


def test_verify_archive_rejects_tampered_tarball(monkeypatch):
    fetched = _tar_bytes({"packaging/x": b"one"}, "katzenqt-0.0.1/", "w:gz")
    local = _tar_bytes({"packaging/x": b"tampered"}, "", "w")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=local),
    )
    with pytest.raises(ValueError, match="does not match"):
        release.verify_archive("v0.0.1", fetched)
