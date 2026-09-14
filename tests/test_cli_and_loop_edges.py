from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import cast

import click
import pytest

from katzenqt import network, persistent
from katzenqt.headless import _cli
from tests.stubs import ignore


@pytest.fixture
def preserved_logging() -> Iterator[None]:
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


def test_parse_turns_a_click_abort_into_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def abort(**kwargs: object) -> object:
        raise click.exceptions.Abort

    monkeypatch.setattr(_cli.verbs, "main", abort)
    with pytest.raises(SystemExit) as caught:
        _cli.parse(["info"])
    assert caught.value.code == 1


@pytest.mark.asyncio
async def test_get_pki_document_is_none_before_a_connection() -> None:
    assert await network.get_pki_document(None) is None


@pytest.mark.asyncio
async def test_get_pki_document_comes_from_the_connection() -> None:
    doc = {"Epoch": 7}

    class Conn:
        def pki_document(self) -> "dict[str, int]":
            return doc

    assert await network.get_pki_document(cast("object", Conn())) == doc


def test_cli_tolerates_a_loop_without_signal_handlers(
    monkeypatch: pytest.MonkeyPatch, preserved_logging: None,
) -> None:
    real_new_loop = asyncio.new_event_loop

    def loop_without_signals() -> asyncio.AbstractEventLoop:
        loop = real_new_loop()

        def refuse(*args: object) -> None:
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", refuse)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", loop_without_signals)
    monkeypatch.setattr(persistent, "init_and_migrate", ignore)
    monkeypatch.delenv("KQT_LOG_LEVEL", raising=False)
    from katzenqt import headless

    assert headless.cli(["info"]) == 0
