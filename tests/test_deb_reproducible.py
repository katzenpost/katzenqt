import os
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "packaging" / "debian" / "reproducible.sh"


def test_reproducible_script_builds_twice_in_a_container_and_compares():
    assert os.access(SCRIPT, os.X_OK)
    body = SCRIPT.read_text()
    assert body.count("build.sh") >= 2
    assert 'test "$first" = "$second"' in body
    assert "packaging/container/Containerfile" in body
    assert "$PODMAN" in body
    assert "reprotest" not in body


def test_reproducible_script_labels_mount_and_drops_stale_dist():
    body = SCRIPT.read_text()
    assert "/src:ro,z" in body
    assert "rm -rf /build/a/dist /build/b/dist" in body


def test_ci_enforces_reproducibility():
    wf = (ROOT / ".github" / "workflows" / "deb-multi-os.yml").read_text()
    assert "builds reproducibly" in wf
    assert 'test "$a" = "$b"' in wf
