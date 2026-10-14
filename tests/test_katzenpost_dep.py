import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
CI = ROOT / "packaging" / "debian" / "ci.sh"
DEP_DEB = ROOT / "packaging" / "container" / "dep-deb.sh"


def test_katzenqt_depends_on_the_katzenpost_package() -> None:
    control = (ROOT / "debian" / "control").read_text()
    assert re.search(r"^\s*katzenpost,\s*$", control, re.MULTILINE)
    assert not re.search(r"^\s*kpclientd,\s*$", control, re.MULTILINE)


def test_nothing_builds_kpclientd_here_any_more() -> None:
    container = ROOT / "packaging" / "container"
    assert not (container / "kpclientd-build.sh").exists()
    assert not (container / "build-kpclientd.sh").exists()
    assert not (container / "Containerfile.kpclientd").exists()
    assert not (ROOT / "packaging" / "debian" / "kpclientd").exists()
    mk = (ROOT / "packaging" / "debian" / "Makefile").read_text()
    assert "kpclientd" not in mk
    rules = (ROOT / "debian" / "rules").read_text()
    assert "kpclientd" not in rules


def test_dependency_debs_come_from_their_own_make_target() -> None:
    assert os.access(DEP_DEB, os.X_OK)
    body = DEP_DEB.read_text()
    assert "deb-build" in body
    assert "SOURCE_DATE_EPOCH" in body
    assert "switch --detach" in body
    assert "kpclientd" not in body
    assert "katzenpost" not in body


def test_ci_builds_and_installs_the_katzenpost_deb() -> None:
    ci = CI.read_text()
    assert "packaging/container/dep-deb.sh" in ci
    assert '"$KATZENPOST_URL" "$KATZENPOST_REV"' in ci
    assert '"$debs"/katzenpost_*.deb' in ci


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="ascii")
    path.chmod(0o755)


def _stubbed_path(tmp_path: Path) -> "dict[str, str]":
    """A PATH where git and make each fail their first call.

    The real tools the script still needs are symlinked beside the
    stubs, so nothing outside this directory is reachable.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    bump = (
        'n=$(( $(cat "$STUB_STATE/%s" 2>/dev/null || echo 0) + 1 ))\n'
        'echo "$n" > "$STUB_STATE/%s"\n'
    )
    _stub(bin_dir / "git", """
next=0
for a in "$@"; do
    if [ "$next" = 1 ]; then dir=$a; next=0; fi
    [ "$a" = "-C" ] && next=1
done
for a in "$@"; do
    case $a in
    clone)
""" + bump % ("clone", "clone") + """        for d in "$@"; do dest=$d; done
        if [ "$n" -eq 1 ]; then
            mkdir -p "$dest"
            : > "$dest/.half-fetched"
            exit 1
        fi
        if [ -e "$dest/.half-fetched" ]; then
            echo "destination path already exists" >&2
            exit 1
        fi
        mkdir -p "$dest"
        exit 0 ;;
    clean)
        rm -rf "$dir/dist"
        exit 0 ;;
    cat-file|switch)
        exit 0 ;;
    esac
done
exit 0
""")
    _stub(bin_dir / "make", bump % ("make", "make") + """
next=0
for a in "$@"; do
    if [ "$next" = 1 ]; then src=$a; next=0; fi
    [ "$a" = "-C" ] && next=1
done
if [ "$n" -eq 1 ]; then
    mkdir -p "$src/dist"
    : > "$src/dist/half-written"
    exit 1
fi
if [ -e "$src/dist/half-written" ]; then
    echo "the last attempt left its wreckage behind" >&2
    exit 1
fi
mkdir -p "$src/dist"
: > "$src/dist/katzenpost_1.0_amd64.deb"
exit 0
""")
    _stub(bin_dir / "dpkg-parsechangelog", "echo 1700000000\n")
    _stub(bin_dir / "sleep", "exit 0\n")
    for name in ("mkdir", "mktemp", "rm", "cp", "dirname", "cat", "echo"):
        source = shutil.which(name)
        assert source is not None, name
        (bin_dir / name).symlink_to(source)
    return {
        "PATH": str(bin_dir),
        "STUB_STATE": str(state),
        "SHELL": "/bin/sh",
    }


def test_a_blip_fetching_the_dependency_does_not_fail_the_build(
    tmp_path: Path,
) -> None:
    """One failed download costs an attempt, not the Debian job.

    The stubs fail the clone once and the build once, which is what a
    proxy.golang.org blip looks like from here. Both are retried, and
    the retry has to start from a clean tree or the second attempt
    fails on the wreckage of the first.
    """
    out = tmp_path / "out"
    env = _stubbed_path(tmp_path)
    done = subprocess.run(
        [
            str(DEP_DEB), str(out),
            "https://example.invalid/katzenpost", "HEAD",
        ],
        capture_output=True, text=True, env=env,
    )
    assert done.returncode == 0, done.stderr
    assert list(out.glob("katzenpost_*.deb")), done.stderr
    assert "retrying" in done.stderr
    state = tmp_path / "state"
    assert (state / "clone").read_text().strip() == "2"
    assert (state / "make").read_text().strip() == "2"
