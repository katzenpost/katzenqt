from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deb-multi-os.yml"


def test_multi_os_ci_covers_both_targets():
    wf = WORKFLOW.read_text()
    assert "debian:13" in wf
    assert "ubuntu:26.04" in wf
    assert "dpkg-buildpackage" in wf
    assert "namenlos" not in wf.lower()
    assert "pip install" not in wf
