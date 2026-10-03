from pathlib import Path

ROOT = Path(__file__).parents[1]
DEBIAN = ROOT / "debian"
CI = ROOT / "packaging" / "debian" / "ci.sh"
SMOKE = ROOT / "packaging" / "debian" / "smoke.sh"


def test_service_installed_as_user_unit_but_not_enabled() -> None:
    """The unit ships with kpclientd now, so katzenqt must not ship one
    of its own; smoke.sh still proves the dependency delivered it."""
    rules = (DEBIAN / "rules").read_text()
    assert "usr/lib/systemd/user" not in rules
    assert "dh_installsystemduser" not in rules
    smoke = SMOKE.read_text()
    assert "test -f /usr/lib/systemd/user/kpclientd.service" in smoke
    assert "test -x /usr/bin/kpclientd" in smoke
    assert "test -f /etc/kpclientd/client.toml" in smoke


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


def test_postinst_prompts_only_when_interactive() -> None:
    postinst = (DEBIAN / "katzenqt.postinst").read_text()
    assert "[ -t 0 ]" in postinst
    assert "systemctl --user enable --now kpclientd" in postinst
    assert "#DEBHELPER#" in postinst


def test_ci_asserts_the_service_is_not_enabled() -> None:
    wf = (ROOT / ".github" / "workflows" / "deb.yml").read_text()
    smoke = SMOKE.read_text()
    assert ".wants/kpclientd.service" in smoke
    assert "test -f /usr/lib/systemd/user/kpclientd.service" in smoke
    assert "packaging/debian/smoke.sh" in CI.read_text()
