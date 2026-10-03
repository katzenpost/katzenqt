from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest

from katzenqt import network
from katzenqt.headless import _actions
from tests.stubs import appending


@pytest.mark.real_sleeps
@pytest.mark.asyncio
async def test_shutdown_overrun_warns_on_the_module_logger(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_join(_tasks: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(network, "_cancel_and_join", slow_join)
    monkeypatch.setattr(
        logging.getLogger("katzen.headless"),
        "propagate",
        True,
    )

    async def idle() -> None:
        await asyncio.sleep(30)

    bg = asyncio.ensure_future(idle())
    stopped: list[bool] = []
    connection = type("C", (), {"stop": appending(stopped, True)})()
    with caplog.at_level(logging.WARNING):
        await _actions._shutdown(
            bg,
            cast("object", connection),
            timeout=0.01,
        )
    bg.cancel()
    assert stopped == [True]
    overruns = [
        r
        for r in caplog.records
        if "did not finish cancelling" in r.getMessage()
    ]
    assert overruns, "the overrun warning was not emitted at all"
    assert overruns[0].name == "katzen.headless"
