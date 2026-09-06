import asyncio

import pytest

from katzenqt.katzen_util import create_task


@pytest.mark.asyncio
async def test_create_task_logs_failure_without_noising_the_event_loop(capsys):
    # The done callback must NOT re-raise the task's exception: a callback
    # raise only surfaces as a spurious asyncio "Exception in callback"
    # traceback (seen on transient kpclientd link drops during a bounce).
    # The printed traceback already preserves visibility.
    loop = asyncio.get_running_loop()
    handler_calls = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, c: handler_calls.append(c))
    try:
        async def boom():
            raise RuntimeError("nope")

        task = create_task(boom())
        with pytest.raises(RuntimeError):
            await task  # the callback already consumed + logged the exception
    finally:
        loop.set_exception_handler(prev_handler)

    assert handler_calls == []
    out, err = capsys.readouterr()
    assert "RuntimeError: nope" in out + err