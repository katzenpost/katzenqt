from collections.abc import Callable, Iterator
from inspect import unwrap
import math
from pathlib import Path
from typing import NoReturn, cast

import pytest

from tests.integration import _bounce_helpers as helpers
from tests.integration import conftest as integration_fixtures


@pytest.fixture(autouse=True)
def clean_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[None]:
    helpers.epoch_duration_s.cache_clear()
    for key in ("KQT_EPOCH_DURATION_S", "KQT_INTEGRATION_TARGET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "KATZENPOST_DOCKER_COMPOSE", str(tmp_path / "missing.yml"),
    )
    yield
    helpers.epoch_duration_s.cache_clear()


def _forbid_docker_read(
    path: Path, *args: object, **kwargs: object,
) -> NoReturn:
    pytest.fail("read Docker config")


def test_live_epoch_is_explicit_without_reading_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KQT_INTEGRATION_TARGET", "namenlos")
    monkeypatch.setenv("KQT_EPOCH_DURATION_S", "1200")
    monkeypatch.setattr(Path, "read_text", _forbid_docker_read)
    assert helpers.epoch_duration_s() == 1200


def test_missing_live_epoch_never_falls_back_to_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KQT_INTEGRATION_TARGET", "namenlos")
    monkeypatch.setattr(Path, "read_text", _forbid_docker_read)
    with pytest.raises(RuntimeError, match="KQT_EPOCH_DURATION_S"):
        helpers.epoch_duration_s()


@pytest.mark.parametrize(
    "value", ["0", "-1", "nan", "inf", "-inf", "1e999", "bad", ""],
)
def test_invalid_override_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("KQT_EPOCH_DURATION_S", value)
    with pytest.raises(ValueError, match="positive finite"):
        helpers.epoch_duration_s()


@pytest.mark.parametrize("duration, seconds", [
    ("30s", 30), ("2m", 120), ("1h30m", 5400), ("10ms", 0.01),
])
def test_docker_epoch_is_parsed_from_generated_service_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    duration: str, seconds: float,
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  kpclientd:\n    environment:\n"
        f"      - KATZENPOST_EPOCH_DURATION={duration}\n",
        encoding="ascii",
    )
    monkeypatch.setenv("KATZENPOST_DOCKER_COMPOSE", str(compose))
    assert math.isclose(helpers.epoch_duration_s(), seconds)


@pytest.mark.parametrize("duration", ["-2m", "2mgarbage", "garbage2m", "0s"])
def test_malformed_docker_duration_is_rejected(duration: str) -> None:
    with pytest.raises(ValueError):
        helpers._parse_go_duration(duration)


def test_missing_epoch_fails_fixture_before_endpoint_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KQT_INTEGRATION_TARGET", "namenlos")

    def probe(host: str, port: int, timeout: float = 2.0) -> NoReturn:
        pytest.fail("probe ran before metadata validation")

    monkeypatch.setattr(integration_fixtures, "_kpclientd_reachable", probe)
    endpoint = cast(
        Callable[[], tuple[str, int]],
        unwrap(integration_fixtures.kpclientd_endpoint),
    )
    with pytest.raises(RuntimeError, match="KQT_EPOCH_DURATION_S"):
        endpoint()
