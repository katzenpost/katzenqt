from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deb-multi-os.yml"


def test_multi_os_ci_covers_both_targets():
    wf = WORKFLOW.read_text()
    assert "debian:13" in wf
    assert "ubuntu:26.04" in wf
    # CI builds the shipped .deb via the same script a developer would run
    # locally, not a hand-inlined dpkg-buildpackage -- so the reproducibility
    # check below can compare against what was actually installed.
    assert "packaging/debian/build.sh" in wf
    assert "namenlos" not in wf.lower()
    assert "pip install" not in wf
