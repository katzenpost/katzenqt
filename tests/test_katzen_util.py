import asyncio
import logging

import pytest

from katzenqt.katzen_util import create_task


@pytest.mark.asyncio
async def test_create_task_logs_failure_without_noising_the_event_loop(caplog):
    # The done callback must NOT re-raise the task's exception: a callback
    # raise only surfaces as a spurious asyncio "Exception in callback"
    # traceback (seen on transient kpclientd link drops during a bounce).
    # logger.error's exc_info already preserves the traceback, through the
    # normal logging configuration rather than a print() invisible in a
    # packaged/windowed build.
    loop = asyncio.get_running_loop()
    handler_calls = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, c: handler_calls.append(c))
    try:
        async def boom():
            raise RuntimeError("nope")

        with caplog.at_level(logging.ERROR, logger="katzen.util"):
            task = create_task(boom())
            with pytest.raises(RuntimeError):
                await task  # the callback already consumed + logged the exception
    finally:
        loop.set_exception_handler(prev_handler)

    assert handler_calls == []
    assert any(
        r.exc_info and isinstance(r.exc_info[1], RuntimeError)
        and str(r.exc_info[1]) == "nope"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_create_task_cancellation_is_not_an_error(caplog):
    async def sleeps_forever():
        await asyncio.Event().wait()

    with caplog.at_level(logging.ERROR, logger="katzen.util"):
        task = create_task(sleeps_forever())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert caplog.records == []