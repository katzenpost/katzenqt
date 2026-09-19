import asyncio
import logging
from types import SimpleNamespace

import pytest

from katzenqt import katzen


def _supervise(window, name, coro_factory, **kwargs):
    # Recursion inside the supervisor resolves back through `self`, which a
    # bare SimpleNamespace cannot supply from the class: bind it once, as the
    # listener-hardening tests do for _process_conversation_update.
    window._supervised_listener = katzen.MainWindow._supervised_listener.__get__(window)
    return window._supervised_listener(name, coro_factory, **kwargs)


async def _await_until(condition, timeout_s: float = 2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _yield_a_few():
    for _ in range(5):
        await asyncio.sleep(0)


class TestSupervisedListener:
    @pytest.mark.asyncio
    async def test_exception_ends_reschedule(self, caplog):
        """A listener that dies with an exception is run again, once."""
        window = SimpleNamespace()
        calls: list[str] = []
        _supervise(window, "probe", lambda: _fail_once(calls), backoff_s=0.0)

        with caplog.at_level(logging.ERROR, logger="katzen"):
            await _await_until(lambda: len(calls) == 2)
        assert calls == ["run", "run"]
        assert any(
            "probe: died with boom" in r.message for r in caplog.records
        )
        # The clean second run must not trigger a third restart.
        await _yield_a_few()
        assert calls == ["run", "run"]

    @pytest.mark.asyncio
    async def test_clean_finish_is_not_restarted_unless_requested(self):
        """A listener that returns normally stays down by default."""
        window = SimpleNamespace()
        calls: list[str] = []
        _supervise(window, "probe", _clean_runner(calls))

        await _await_until(lambda: len(calls) == 1)
        await _yield_a_few()
        assert calls == ["run"]


async def _fail_once(calls):
    calls.append("run")
    if len(calls) == 1:
        raise ValueError("boom")


def _clean_runner(calls):
    async def run():
        calls.append("run")

    return run