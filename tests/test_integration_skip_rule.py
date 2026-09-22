from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.integration import conftest as integration_conftest

TESTS = Path(__file__).resolve().parent
INTEGRATION = TESTS / "integration"


class _Item:
    def __init__(self, path: Path) -> None:
        self.fspath = path
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def _classify(monkeypatch: pytest.MonkeyPatch, *paths: Path) -> list[bool]:
    monkeypatch.delenv("KATZENQT_DOCKER_INTEGRATION", raising=False)
    items = [_Item(p) for p in paths]
    integration_conftest.pytest_collection_modifyitems(
        cast(pytest.Config, None),
        cast("list[pytest.Item]", items),
    )
    return [bool(item.markers) for item in items]


def test_a_test_under_the_integration_directory_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _classify(monkeypatch, INTEGRATION / "test_voucher.py") == [True]


def test_a_test_merely_named_for_integration_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _classify(monkeypatch, TESTS / "test_integration_budgets.py") == [
        False
    ]


def test_the_docker_run_skips_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KATZENQT_DOCKER_INTEGRATION", "1")
    item = _Item(INTEGRATION / "test_voucher.py")
    integration_conftest.pytest_collection_modifyitems(
        cast(pytest.Config, None),
        cast("list[pytest.Item]", [item]),
    )
    assert item.markers == []
