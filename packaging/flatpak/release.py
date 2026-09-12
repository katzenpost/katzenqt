import argparse
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


APP_ID = "network.katzenpost.katzenqt"
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "packaging" / "flatpak"
TAG_PATTERN = re.compile(r"v?(\d+\.\d+\.\d+)")


def run(*args, cwd=ROOT, capture=True):
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    if capture:
        return result.stdout.strip()
    return ""


def tag_details(tag, remote=False):
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise ValueError(
            "TAG must have the form MAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH"
        )
    if run("git", "cat-file", "-t", tag) != "tag":
        raise ValueError(f"{tag} is not an annotated tag")
    commit = run("git", "rev-parse", f"{tag}^{{commit}}")
    if run("git", "rev-parse", "HEAD") != commit:
        raise ValueError("HEAD does not match TAG")
    if run("git", "status", "--porcelain"):
        raise ValueError("the worktree is dirty")
    if remote:
        run("git", "fetch", "origin", "main", capture=False)
        main_ref = run("git", "rev-parse", "FETCH_HEAD")
    else:
        main_ref = "main"
    run("git", "merge-base", "--is-ancestor", commit, main_ref)
    version = match.group(1)
    project = tomllib.loads(run("git", "show", f"{tag}:pyproject.toml"))
    if project["project"]["version"] != version:
        raise ValueError("pyproject version does not match TAG")
    root = ET.fromstring(
        run("git", "show", f"{tag}:packaging/flatpak/{APP_ID}.metainfo.xml")
    )
    release = root.find("./releases/release")
    date = (
        datetime.fromtimestamp(
            int(run("git", "log", "-1", "--format=%ct", tag)), timezone.utc
        )
        .date()
        .isoformat()
    )
    if release is None or release.get("version") != version:
        raise ValueError("AppStream version does not match TAG")
    if release.get("date") != date:
        raise ValueError("AppStream release date does not match TAG")
    if remote:
        run("git", "ls-remote", "--exit-code", "origin", f"refs/tags/{tag}")
    return commit, version, date


def _tar_files(fileobj, mode, strip):
    tree = {}
    with tarfile.open(fileobj=fileobj, mode=mode) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if strip:
                head, _, rest = name.partition("/")
                if not rest:
                    continue
                name = rest
            extracted = tar.extractfile(member)
            tree[name] = extracted.read() if extracted else b""
    return tree


def verify_archive(tag, data):
    fetched = _tar_files(io.BytesIO(data), "r:gz", strip=True)
    local = subprocess.run(
        ["git", "archive", "--format=tar", tag],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    if fetched != _tar_files(io.BytesIO(local), "r:", strip=False):
        raise ValueError(
            "the fetched release tarball does not match git archive of the tag"
        )


def archive(tag):
    base = "https://github.com/katzenpost/katzenqt/archive/refs/tags"
    url = f"{base}/{tag}.tar.gz"
    with urllib.request.urlopen(url) as response:
        data = response.read()
    verify_archive(tag, data)
    return url, hashlib.sha256(data).hexdigest()


def tagged_file(tag, path):
    return subprocess.run(
        ["git", "show", f"{tag}:{path}"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def stage(tag, destination, remote=False):
    tag_details(tag, remote)
    url, digest = archive(tag)
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    paths = [
        "packaging/flatpak/LICENSE",
        "packaging/flatpak/README.md",
        "packaging/flatpak/flathub.json",
        "packaging/flatpak/pyside6-sources.json",
        "packaging/flatpak/python3-deps.json",
        "packaging/flatpak/rustic-audio-tool.json",
        "packaging/flatpak/rustic-audio-sources.json",
        f"packaging/flatpak/{APP_ID}.metainfo.xml",
        f"packaging/{APP_ID}.desktop",
        "packaging/flatpak/screenshots/katzenqt.png",
    ]
    for path in paths:
        target = destination / Path(path).name
        if "/screenshots/" in path:
            target = destination / "screenshots" / target.name
            target.parent.mkdir()
        target.write_bytes(tagged_file(tag, path))
    metainfo = destination / f"{APP_ID}.metainfo.xml"
    metainfo.write_text(
        metainfo.read_text().replace(
            "/main/packaging/flatpak/screenshots/",
            f"/{tag}/packaging/flatpak/screenshots/",
        )
    )
    manifest = tagged_file(tag, f"packaging/flatpak/{APP_ID}.yaml").decode()
    source_needle = (
        "      - type: archive\n        path: katzenqt-src.tar.gz\n"
    )
    if source_needle not in manifest:
        raise ValueError("manifest local source block not found; formatting drifted")
    manifest = manifest.replace(
        source_needle,
        f"      - type: archive\n        url: {url}\n"
        f"        sha256: {digest}\n",
    )
    desktop_needle = f"path: ../{APP_ID}.desktop"
    if desktop_needle not in manifest:
        raise ValueError("manifest desktop path not found; formatting drifted")
    manifest = manifest.replace(desktop_needle, f"path: {APP_ID}.desktop")
    (destination / f"{APP_ID}.yaml").write_text(manifest)
    return destination


def submit(tag, destination):
    marker = Path(destination) / ".checked-tag"
    if not marker.is_file() or marker.read_text().strip() != tag:
        raise ValueError(
            "run check before submit: .checked-tag is missing or does not match"
        )
    tag_details(tag, True)
    stage(tag, destination, True)
    run("gh", "auth", "status", capture=False)
    existing = (
        subprocess.run(
            ["gh", "repo", "view", f"flathub/{APP_ID}"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    target = f"flathub/{APP_ID}" if existing else "flathub/flathub"
    base = "master" if existing else "new-pr"
    branch = f"release-{tag}"
    with tempfile.TemporaryDirectory() as temp:
        checkout = Path(temp) / "checkout"
        run(
            "gh", "repo", "fork", "--clone", target, "--", str(checkout),
            capture=False,
        )
        run(
            "git", "checkout", "-B", branch, f"origin/{base}",
            cwd=checkout, capture=False,
        )
        for path in checkout.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for source in Path(destination).iterdir():
            target_path = checkout / source.name
            if source.is_dir():
                shutil.copytree(source, target_path, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target_path)
        run("git", "add", ".", cwd=checkout, capture=False)
        run(
            "git", "commit", "-m", f"Release {APP_ID} {tag}",
            cwd=checkout, capture=False,
        )
        run("git", "push", "-u", "origin", branch, cwd=checkout, capture=False)
        title = f"Update {APP_ID} to {tag}" if existing else f"Add {APP_ID}"
        run(
            "gh", "pr", "create", "--repo", target, "--base", base,
            "--title", title, "--body",
            f"Tagged upstream release {tag}. Tested with "
            f"make flatpak-release TAG={tag}.",
            cwd=checkout, capture=False,
        )


def check(tag, destination):
    dest = Path(destination)
    fs = f"--filesystem={ROOT}"
    run("flatpak", "run", fs, "--command=flathub-build",
        "org.flatpak.Builder", f"{APP_ID}.yaml",
        cwd=dest, capture=False)
    run("flatpak", "run", fs, "--command=flatpak-builder-lint",
        "org.flatpak.Builder", "manifest", f"{APP_ID}.yaml",
        cwd=dest, capture=False)
    run("flatpak", "run", fs, "--command=flatpak-builder-lint",
        "org.flatpak.Builder", "repo", "repo",
        cwd=dest, capture=False)
    (dest / ".checked-tag").write_text(f"{tag}\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["validate", "dist", "check", "submit"]
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--destination", default=".flathub-dist")
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            tag_details(args.tag, args.remote)
        elif args.action == "dist":
            stage(args.tag, args.destination, args.remote)
        elif args.action == "check":
            check(args.tag, args.destination)
        else:
            submit(args.tag, args.destination)
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
