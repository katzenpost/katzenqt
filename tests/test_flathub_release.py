import importlib.util
import io
import subprocess
import tarfile
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
VERSION = PROJECT["project"]["version"]
TAG = f"v{VERSION}"
BUILD_LINES = (ROOT / "packaging/flatpak/build.sh").read_text().splitlines()
EPOCH = int(
    next(line[6:] for line in BUILD_LINES if line.startswith("epoch="))
)
RELEASE_DATE = datetime.fromtimestamp(EPOCH, timezone.utc).date().isoformat()
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
            ("git", "cat-file", "-t", TAG): "tag",
            ("git", "rev-parse", f"{TAG}^{{commit}}"): "abc",
            ("git", "rev-parse", "HEAD"): "abc",
            ("git", "status", "--porcelain"): "",
            ("git", "rev-parse", "FETCH_HEAD"): "def",
            (
                "git",
                "show",
                f"{TAG}:pyproject.toml",
            ): f'[project]\nversion = "{VERSION}"',
            (
                "git",
                "show",
                f"{TAG}:packaging/flatpak/"
                "network.katzenpost.katzenqt.metainfo.xml",
            ): (
                f'<component><releases><release version="{VERSION}" '
                f'date="{RELEASE_DATE}"/></releases></component>'
            ),
            ("git", "log", "-1", "--format=%ct", TAG): str(EPOCH),
            (
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/tags/{TAG}",
            ): "abc",
        }
        return values.get(args, "")

    monkeypatch.setattr(release, "run", fake_run)
    assert release.tag_details(TAG, True) == (
        "abc",
        VERSION,
        RELEASE_DATE,
    )
    assert ("git", "fetch", "origin", "main") in calls
    assert ("git", "merge-base", "--is-ancestor", "abc", "def") in calls


def test_tag_details_rejects_lightweight_tag(monkeypatch):
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: "commit")
    with pytest.raises(ValueError, match="annotated"):
        release.tag_details(TAG)


def test_tag_details_requires_tag_checkout(monkeypatch):
    values = {
        ("git", "cat-file", "-t", VERSION): "tag",
        ("git", "rev-parse", f"{VERSION}^{{commit}}"): "tagged",
        ("git", "rev-parse", "HEAD"): "other",
    }
    monkeypatch.setattr(
        release, "run", lambda *args, **kwargs: values.get(args, "")
    )
    with pytest.raises(ValueError, match="HEAD"):
        release.tag_details(VERSION)


def test_tag_details_accepts_repository_tag_style(monkeypatch):
    monkeypatch.setattr(
        release,
        "run",
        lambda *args, **kwargs: {
            ("git", "cat-file", "-t", VERSION): "tag",
            ("git", "rev-parse", f"{VERSION}^{{commit}}"): "abc",
            ("git", "rev-parse", "HEAD"): "abc",
            ("git", "status", "--porcelain"): "",
            (
                "git",
                "show",
                f"{VERSION}:pyproject.toml",
            ): f'[project]\nversion = "{VERSION}"',
            (
                "git",
                "show",
                f"{VERSION}:packaging/flatpak/"
                "network.katzenpost.katzenqt.metainfo.xml",
            ): (
                f'<component><releases><release version="{VERSION}" '
                f'date="{RELEASE_DATE}"/></releases></component>'
            ),
            ("git", "log", "-1", "--format=%ct", VERSION): str(EPOCH),
        }.get(args, ""),
    )
    assert release.tag_details(VERSION)[1] == VERSION


def test_stage_contains_only_tagged_application_source(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release, "tag_details", lambda *args: ("abc", VERSION, RELEASE_DATE)
    )
    monkeypatch.setattr(
        release,
        "archive",
        lambda tag: (f"https://example/{TAG}.tar.gz", "a" * 64),
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
    destination = release.stage(TAG, tmp_path / "dist")
    assert (
        destination / "rustic-audio-tool.json"
    ).read_bytes() == b"audio-module"
    assert (
        destination / "rustic-audio-sources.json"
    ).read_bytes() == b"audio-sources"
    manifest = (destination / f"{release.APP_ID}.yaml").read_text()
    assert f"https://example/{TAG}.tar.gz" in manifest
    assert f"sha256: {'a' * 64}" in manifest
    assert "../../" not in manifest
    assert f"path: {release.APP_ID}.desktop" in manifest
    assert (
        f"/{TAG}/packaging/flatpak/screenshots/"
        in (destination / f"{release.APP_ID}.metainfo.xml").read_text()
    )

    second = release.stage(TAG, tmp_path / "second")
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
        release, "tag_details", lambda *args: ("abc", VERSION, RELEASE_DATE)
    )
    monkeypatch.setattr(
        release,
        "archive",
        lambda tag: (f"https://example/{VERSION}.tar.gz", "a" * 64),
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
    destination = release.stage(VERSION, tmp_path / "dist")
    assert (
        (destination / f"{release.APP_ID}.yaml")
        .read_bytes()
        .startswith(b"app-id:")
    )


def test_submit_opens_pr_without_merging(tmp_path, monkeypatch):
    calls = []
    destination = tmp_path / "dist"
    destination.mkdir()
    (destination / ".checked-tag").write_text(f"{TAG}\n")

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
    release.submit(TAG, destination)
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
        release.submit(TAG, destination)
    (destination / ".checked-tag").write_text("v9.9.9\n")
    with pytest.raises(ValueError, match="check before submit"):
        release.submit(TAG, destination)


def test_stage_fails_when_manifest_formatting_drifts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        release, "tag_details", lambda *args: ("abc", VERSION, RELEASE_DATE)
    )
    monkeypatch.setattr(
        release,
        "archive",
        lambda tag: (f"https://example/{VERSION}.tar.gz", "a" * 64),
    )
    monkeypatch.setattr(
        release, "tagged_file", lambda tag, path: b"app-id: drift\n"
    )
    with pytest.raises(ValueError, match="formatting drifted"):
        release.stage(VERSION, tmp_path / "dist")


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
    fetched = _tar_bytes(files, f"katzenqt-{VERSION}/", "w:gz")
    local = _tar_bytes(files, "", "w")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=local),
    )
    release.verify_archive(TAG, fetched)


def test_verify_archive_rejects_tampered_tarball(monkeypatch):
    fetched = _tar_bytes(
        {"packaging/x": b"one"}, f"katzenqt-{VERSION}/", "w:gz"
    )
    local = _tar_bytes({"packaging/x": b"tampered"}, "", "w")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=local),
    )
    with pytest.raises(ValueError, match="does not match"):
        release.verify_archive(TAG, fetched)


def test_current_release_metadata_matches_project_and_build_epoch():
    metadata = ET.parse(release.PACKAGE / f"{release.APP_ID}.metainfo.xml")
    current = metadata.find("./releases/release")
    assert current is not None
    assert current.get("version") == VERSION
    assert current.get("date") == RELEASE_DATE


@pytest.mark.parametrize(
    ("mismatch", "expected"),
    [
        ("project", "pyproject version"),
        ("metadata_version", "AppStream version"),
        ("metadata_date", "AppStream release date"),
    ],
)
def test_tag_details_rejects_release_metadata_drift(
    monkeypatch, mismatch, expected
):
    version = VERSION + ".invalid"
    project_version = version if mismatch == "project" else VERSION
    metadata_version = version if mismatch == "metadata_version" else VERSION
    metadata_date = "invalid" if mismatch == "metadata_date" else RELEASE_DATE
    values = {
        ("git", "cat-file", "-t", TAG): "tag",
        ("git", "rev-parse", f"{TAG}^{{commit}}"): "abc",
        ("git", "rev-parse", "HEAD"): "abc",
        ("git", "show", f"{TAG}:pyproject.toml"): (
            f'[project]\nversion = "{project_version}"'
        ),
        (
            "git",
            "show",
            f"{TAG}:packaging/flatpak/{release.APP_ID}.metainfo.xml",
        ): (
            f'<component><releases><release version="{metadata_version}" '
            f'date="{metadata_date}"/></releases></component>'
        ),
        ("git", "log", "-1", "--format=%ct", TAG): str(EPOCH),
    }
    monkeypatch.setattr(
        release, "run", lambda *args, **kwargs: values.get(args, "")
    )
    with pytest.raises(ValueError, match=expected):
        release.tag_details(TAG)
