from __future__ import annotations

import runpy
import sys

import pytest

import katzenqt
from katzenqt import integration_runner


def test_cli_is_exposed_lazily() -> None:
    assert callable(katzenqt.cli)
    assert "cli" in katzenqt.__all__


def test_an_unknown_attribute_still_raises() -> None:
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        katzenqt.nope


def test_integration_runner_delegates_to_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str] | None] = []

    def fake_cli(argv: "list[str] | None" = None) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(integration_runner.headless, "cli", fake_cli)
    assert integration_runner.main(["info"]) == 7
    assert seen == [["info"]]


def test_integration_runner_passes_none_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str] | None] = []

    def fake_cli(argv: "list[str] | None" = None) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(integration_runner.headless, "cli", fake_cli)
    assert integration_runner.main() == 0
    assert seen == [None]


def test_headless_module_entry_point_exits_with_the_cli_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_cli(argv: "list[str] | None" = None) -> int:
        return 7

    monkeypatch.setattr(integration_runner.headless, "cli", fake_cli)
    monkeypatch.setattr(sys, "argv", ["katzenqt-headless"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("katzenqt.headless", run_name="__main__")
    assert caught.value.code == 7


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_integration_runner_module_entry_point_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_cli(argv: "list[str] | None" = None) -> int:
        return 7

    monkeypatch.setattr(integration_runner.headless, "cli", fake_cli)
    monkeypatch.setattr(sys, "argv", ["katzenqt.integration_runner"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("katzenqt.integration_runner", run_name="__main__")
    assert caught.value.code == 7
