import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "packaging" / "flatpak" / "network.katzenpost.katzenqt.yaml"


def test_manifest_pins_source_and_allows_no_network():
    data = MANIFEST.read_text()
    assert re.search(r"katzenqt/archive/[0-9a-f]{40}\.tar\.gz", data)
    assert re.search(r"sha256: [0-9a-f]{64}", data)
    assert "--share=network" not in data
    assert "--socket=fallback-x11" in data
    assert "--socket=pulseaudio" not in data
    assert json.loads(
        (ROOT / "packaging" / "flatpak" / "flathub.json").read_text()
    ) == {"skip-arches": ["aarch64"]}
    assert "--filesystem=xdg-run/katzenpost:ro" in data
    assert "--talk-name=network.katzenpost.kpclientd" in data
    assert "--filesystem=home" not in data
    assert "--filesystem=host" not in data


def test_manifest_does_not_bundle_kpclientd():
    data = MANIFEST.read_text()
    assert "go build" not in data
    assert "install -Dm755 kpclientd" not in data
    assert "/app/bin/kpclientd" not in data


def test_metadata_ids_match():
    app_id = "network.katzenpost.katzenqt"
    manifest = MANIFEST.read_text()
    desktop = (ROOT / "packaging" / f"{app_id}.desktop").read_text()
    metainfo = (
        ROOT / "packaging" / "flatpak" / f"{app_id}.metainfo.xml"
    ).read_text()
    assert f"app-id: {app_id}" in manifest
    assert f"<id>{app_id}</id>" in metainfo
    assert '<developer id="network.katzenpost">' in metainfo
    assert "<name>Katzenpost</name>" in metainfo
    assert "Exec=katzenqt" in desktop
    assert "Experimental" in metainfo
    for warning in (
        "security",
        "anonymity",
        "privacy",
        "reliability",
        "message delivery",
        "data retention",
    ):
        assert warning in metainfo


def test_screenshot_is_real_and_pinned():
    screenshot = (
        ROOT / "packaging" / "flatpak" / "screenshots" / "katzenqt.png"
    )
    metainfo = (
        ROOT
        / "packaging"
        / "flatpak"
        / "network.katzenpost.katzenqt.metainfo.xml"
    ).read_text()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert screenshot.stat().st_size > 40_000
    assert "/main/packaging/flatpak/screenshots/katzenqt.png" in metainfo


def test_docker_start_waits_for_mixnet_readiness():
    makefile = (ROOT / "Makefile").read_text()
    recipe = re.search(
        r"^flatpak-docker-start:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE
    ).group(1)
    assert "$(MAKE) -C $(KATZENPOST_DIR)/docker start wait" in recipe
    assert "socket.create_connection" not in recipe


def test_docker_tests_stop_only_the_mixnet_they_start():
    makefile = (ROOT / "Makefile").read_text()
    recipe = re.search(
        r"^flatpak-test-docker:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE
    ).group(1)
    assert "$(MAKE) flatpak-docker-existing-check" in recipe
    assert "trap cleanup EXIT INT TERM" in recipe
    assert "$(MAKE) -C $(KATZENPOST_DIR)/docker stop" in recipe
    assert "managed test mixnet is still reachable" in recipe
    assert recipe.index("trap cleanup") < recipe.index(
        "$(KATZENPOST_DIR)/docker start wait"
    )
    assert recipe.index("flatpak-docker-check") < recipe.index(
        "$(MAKE) setup-uv"
    )


def test_docker_tests_run_the_installed_flatpak_once():
    makefile = (ROOT / "Makefile").read_text()
    wrapper = (
        ROOT / "packaging" / "flatpak" / "integration-python"
    ).read_text()
    recipe = re.search(
        r"^flatpak-test-docker:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE
    ).group(1)
    assert (
        "KATZENQT_INTEGRATION_PYTHON=$(CURDIR)/packaging/flatpak/"
        "integration-python" in recipe
    )
    assert "packaging/flatpak/wait-for-mixnet" in recipe
    assert "--no-cov tests/integration" in recipe
    assert "flatpak run --die-with-parent" in wrapper
    unit_recipe = (
        "test-uv: $(STAMP_UV)\n"
        "\t@env -u KATZENQT_DOCKER_INTEGRATION $(UV) run pytest "
        '-m "not integration"'
    )
    assert unit_recipe in makefile


def test_service_removes_only_stale_socket_before_restart():
    service = (ROOT / "config" / "kpclientd.service").read_text()
    assert (
        "ExecStartPre=/usr/bin/rm -f %t/katzenpost/kpclientd.sock" in service
    )
    assert "RuntimeDirectoryPreserve=yes" in service
