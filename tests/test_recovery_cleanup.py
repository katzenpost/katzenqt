import asyncio
import threading

import pytest

from katzenqt import network, persistent


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "reply", "reconnect"])
async def test_rpc_cleans_up_owned_tasks(outcome):
    before = asyncio.all_tasks()
    started = asyncio.Event()
    released = asyncio.Event()

    async def rpc():
        started.set()
        try:
            if outcome == "reply":
                network._reconnect_event.set()
                return 42
            await asyncio.Event().wait()
        finally:
            released.set()

    task = asyncio.create_task(network._rpc_racing_connection_life(
        bacap_uuid="test", what="test", rpc_factory=rpc,
        backstop_s=0.02, grace_s=0.01,
    ))
    await started.wait()
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "reply":
        assert await task == 42
    else:
        if outcome == "reconnect":
            network._reconnect_event.set()
        with pytest.raises(network.ConnectionLifeInterruptedError):
            await task
    assert released.is_set()
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "connected", "shutdown"])
async def test_connection_gate_cleans_up_waiters(outcome):
    before = asyncio.all_tasks()
    task = asyncio.create_task(network._wait_for_connection_or_shutdown(
        idle_retry_s=0.01,
    ))
    await asyncio.sleep(0)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if outcome == "connected":
            network.__mixnet_connected.set()
        elif outcome == "shutdown":
            network.__should_quit.set()
        assert await task == (outcome != "shutdown")
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_idle_resend_sweeps_leave_no_waiters(fake_thinclient, monkeypatch):
    before = asyncio.all_tasks()
    monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.001)
    network.__mixnet_connected.set()
    task = asyncio.create_task(network.send_resendable_plaintexts(fake_thinclient))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.03)
    finally:
        network.__should_quit.set()
        network.resendable_event.set()
        await asyncio.wait_for(task, timeout=1)
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_thread_commit_finishes_before_cancellation_returns():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def commit():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("test did not release commit")
        finished.set()
        return 42

    task = asyncio.create_task(persistent._finish_thread(commit))
    try:
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
