from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from katzenqt import headless, network, persistent
from tests.stubs import ignore, returning


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    loggers = [logging.getLogger(n)
               for n in sorted(logging.root.manager.loggerDict)]
    loggers.append(logging.root)
    saved = {
        lg: (list(lg.handlers), lg.level, lg.propagate, lg.disabled)
        for lg in loggers
    }
    try:
        yield
    finally:
        for lg, (handlers, level, propagate, disabled) in saved.items():
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate
            lg.disabled = disabled


@pytest.fixture
def _no_migrate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistent, "init_and_migrate", ignore)


@pytest.fixture
def _restore_echo() -> Iterator[None]:
    before = (persistent._engine.echo, persistent._engine_sync.echo)
    yield
    persistent._engine.echo, persistent._engine_sync.echo = before


@pytest.mark.asyncio
async def test_connect_delegates_to_network_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    sentinel = object()

    async def fake_reconnect(config_path: object = None) -> object:
        seen.append(config_path)
        return sentinel

    monkeypatch.setattr(network, "reconnect", fake_reconnect)
    assert await headless.connect("/tmp/thinclient.toml") is sentinel
    assert seen == ["/tmp/thinclient.toml"]


def test_log_level_override_sets_the_root_level(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
) -> None:
    monkeypatch.setenv("KQT_LOG_LEVEL", "debug")
    headless._configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_an_unknown_log_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
) -> None:
    monkeypatch.setenv("KQT_LOG_LEVEL", "not-a-level")
    headless._configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_quiet_default_clamps_the_handlers(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
) -> None:
    monkeypatch.delenv("KQT_LOG_LEVEL", raising=False)
    headless._configure_logging()
    assert logging.getLogger().level == logging.WARNING
    assert all(h.level == logging.WARNING for h in logging.root.handlers)


def test_verbose_mode_turns_on_engine_echo(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
    _restore_echo: None, _no_migrate: None,
) -> None:
    monkeypatch.setenv("KQT_LOG_LEVEL", "INFO")
    persistent._engine.echo = False
    persistent._engine_sync.echo = False
    assert headless.cli(["info"]) == 0
    assert persistent._engine.echo is True
    assert persistent._engine_sync.echo is True


def test_quiet_mode_leaves_engine_echo_off(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
    _restore_echo: None, _no_migrate: None,
) -> None:
    monkeypatch.delenv("KQT_LOG_LEVEL", raising=False)
    persistent._engine.echo = True
    persistent._engine_sync.echo = True
    assert headless.cli(["info"]) == 0
    assert persistent._engine.echo is False
    assert persistent._engine_sync.echo is False


def test_a_vanished_temp_config_does_not_break_the_exit(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
    _restore_echo: None, _no_migrate: None, tmp_path: Path,
) -> None:
    temp = tmp_path / "thinclient.toml"
    temp.write_text("")

    def fake_resolve(args: object) -> "tuple[str, str]":
        return (str(temp), str(temp))

    def exploding_remove(path: str) -> None:
        raise OSError("already gone")

    monkeypatch.setattr(
        headless._actions, "resolve_connection_config", fake_resolve,
    )
    monkeypatch.setattr(headless._actions, "set_connection_config", ignore)
    monkeypatch.setattr(os, "remove", exploding_remove)
    monkeypatch.delenv("KQT_LOG_LEVEL", raising=False)
    assert headless.cli(["info"]) == 0


def test_stragglers_are_cancelled_and_a_gone_temp_config_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, _restore_logging: None,
    _restore_echo: None, _no_migrate: None, tmp_path: Path,
) -> None:
    import asyncio

    temp = tmp_path / "thinclient.toml"
    temp.write_text("")
    lingering: list[asyncio.Task[None]] = []

    async def forever() -> None:
        await asyncio.sleep(3600)

    async def fake_action(args: object) -> int:
        lingering.append(asyncio.ensure_future(forever()))
        return 0

    removed: list[str] = []

    def exploding_remove(path: str) -> None:
        removed.append(path)
        raise OSError("already gone")

    monkeypatch.setattr(headless._actions, "_action_create_conv", fake_action)
    monkeypatch.setattr(
        headless._actions, "resolve_connection_config",
        returning((str(temp), str(temp))),
    )
    monkeypatch.setattr(
        headless._actions, "set_connection_config", ignore,
    )
    monkeypatch.setattr(os, "remove", exploding_remove)
    monkeypatch.delenv("KQT_LOG_LEVEL", raising=False)

    rc = headless.cli(
        ["create-conv", "--config", str(temp), "demo", "me"],
    )
    assert rc == 0
    assert removed == [str(temp)]
    assert lingering and lingering[0].cancelled()
