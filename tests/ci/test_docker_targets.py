from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _recipe(target: str) -> str:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    start = text.index(f"\n{target}:") + 1
    end = text.index("\n\n", start)
    return text[start:end]


def test_mixnet_up_starts_the_katzenpost_docker_mixnet() -> None:
    recipe = _recipe("mixnet-up")
    assert "$(KATZENPOST_DIR)" in recipe.splitlines()[0]
    assert "$(MAKE) -C $(KATZENPOST_DIR)/docker start" in recipe


def test_mixnet_down_stops_it() -> None:
    assert "$(MAKE) -C $(KATZENPOST_DIR)/docker stop" in _recipe(
        "mixnet-down"
    )


def test_run_docker_names_the_instance_and_the_docker_daemon() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nINSTANCE ?= alice\n" in text
    recipe = _recipe("run-docker")
    assert "KQT_STATE=docker-$(INSTANCE)" in recipe
    assert (
        "KATZENQT_THINCLIENT_CONFIG=$(CURDIR)/config/thinclient.docker.toml"
        in recipe
    )
    assert recipe.rstrip().endswith("$(MAKE) run")


def test_the_docker_daemon_config_dials_the_mixnet_kpclientd_over_tcp() -> (
    None
):
    config = tomllib.loads(
        (ROOT / "config" / "thinclient.docker.toml").read_text(
            encoding="ascii"
        ),
    )
    assert config["Dial"]["Tcp"] == {
        "Network": "tcp",
        "Address": "127.0.0.1:64331",
    }
    assert "Unix" not in config["Dial"]


def test_the_readme_walks_through_two_instances() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "make mixnet-up" in text
    assert "make run-docker INSTANCE=alice" in text
    assert "make run-docker INSTANCE=bob" in text
    assert "make mixnet-down" in text
