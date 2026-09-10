from pathlib import Path

ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"


def test_service_installed_as_user_unit_but_not_enabled():
    rules = (DEBIAN / "rules").read_text()
    assert "usr/lib/systemd/user" in rules
    assert "/usr/bin/kpclientd" in rules
    assert "/etc/kpclientd/client.toml" in rules
    assert "dh_installsystemduser --no-enable" in rules


def test_user_preset_enables_by_default_per_user():
    rules = (DEBIAN / "rules").read_text()
    assert "usr/lib/systemd/user-preset" in rules
    assert "enable kpclientd.service" in rules


def test_postinst_prompts_only_when_interactive():
    postinst = (DEBIAN / "katzenqt.postinst").read_text()
    assert "[ -t 0 ]" in postinst
    assert "systemctl --user enable --now kpclientd" in postinst
    assert "#DEBHELPER#" in postinst


def test_ci_asserts_the_service_is_not_enabled():
    wf = (ROOT / ".github" / "workflows" / "deb-multi-os.yml").read_text()
    assert ".wants/kpclientd.service" in wf
    assert "test -f /usr/lib/systemd/user/kpclientd.service" in wf
