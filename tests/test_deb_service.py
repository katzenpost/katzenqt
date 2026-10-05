from pathlib import Path

ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"
CI = ROOT / "packaging" / "debian" / "ci.sh"
TEST_SH = ROOT / "packaging" / "debian" / "test.sh"


def test_the_daemon_and_its_unit_come_from_the_katzenpost_package() -> None:
    control = (DEBIAN / "control").read_text()
    rules = (DEBIAN / "rules").read_text()
    assert "katzenpost," in control
    assert "kpclientd," not in control
    assert "systemd" not in rules
    assert "kpclientd" not in rules


def test_no_user_preset_ships() -> None:
    """kpclientd must stay opt-in per user (see --no-enable and the postinst
    prompt below). A shipped user-preset saying "enable kpclientd.service"
    is inert through this package's own install path but a live footgun the
    moment anything runs `systemctl --user preset-all` /
    `systemctl --global preset-all` against a system with this package
    installed -- that reads exactly such a file and would silently enable a
    mixnet-dialing daemon for every user."""
    rules = (DEBIAN / "rules").read_text()
    assert "usr/lib/systemd/user-preset" not in rules
    assert "enable kpclientd.service" not in rules
    assert not list(DEBIAN.glob("*.user-preset"))


def test_postinst_prompts_only_when_interactive() -> None:
    postinst = (DEBIAN / "katzenqt.postinst").read_text()
    assert "[ -t 0 ]" in postinst
    assert "systemctl --user enable --now kpclientd" in postinst
    assert "#DEBHELPER#" in postinst


def test_ci_asserts_the_service_is_not_enabled() -> None:
    wf = (ROOT / ".github" / "workflows" / "deb.yml").read_text()
    smoke = TEST_SH.read_text()
    assert ".wants/kpclientd.service" in smoke
    assert "test -f /usr/lib/systemd/user/kpclientd.service" in smoke
    assert "packaging/debian/test.sh" in CI.read_text()
