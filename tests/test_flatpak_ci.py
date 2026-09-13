from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "flatpak.yml"


def test_flatpak_ci_runs_the_documented_targets():
    body = WORKFLOW.read_text()
    assert "make flatpak-system-deps" in body
    assert "make flatpak-build" in body


def test_flatpak_ci_uses_apt_not_apt_get():
    body = WORKFLOW.read_text()
    assert "apt-get" not in body


def test_flatpak_ci_has_no_dbus():
    body = WORKFLOW.read_text().lower()
    assert "dbus" not in body
