from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Literal, Self, cast
from uuid import UUID

import pytest

from katzenqt import headless, network
from katzenqt.headless import _actions

if TYPE_CHECKING:
    from katzenqt._thinclient import ThinClient

pytestmark = [pytest.mark.asyncio, pytest.mark.real_sleeps]


@dataclass
class _Client:
    released: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    closed_after_release: bool = False

    def stop(self) -> None:
        self.closed_after_release = self.released.is_set()
        self.closed = True


@dataclass
class _Rows:
    rows: list[SimpleNamespace]

    def all(self) -> list[SimpleNamespace]:
        return self.rows


def _dispatch_state(
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["read", "write", "encrypt"],
) -> None:
    row = SimpleNamespace(
        id=UUID(int=2), bacap_stream=UUID(int=1),
        is_read=kind == "read", indirection=None, bacap_payload=b"Fdata",
    )

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None,
            exc: BaseException | None, tb: TracebackType | None,
        ) -> None:
            pass

        async def exec(self, query: str) -> _Rows:
            selected = "plaintext" if kind == "encrypt" else "mixwal"
            return _Rows([row] if query == selected else [])

        async def get(self, model: object, key: object) -> SimpleNamespace:
            return SimpleNamespace(read_cap=b"r" * 136, read_paused=False)

        async def commit(self) -> None:
            pass

    async def empty_queue() -> set[UUID]:
        return set()

    def mixwal_query(excluded: set[UUID]) -> str:
        return "mixwal"

    def plaintext_query(excluded: set[UUID]) -> str:
        return "plaintext"

    monkeypatch.setattr(network, "persistent", SimpleNamespace(
        asession=Session, ReadCapWAL=object,
        MixWAL=SimpleNamespace(
            get_new=mixwal_query, resend_queue_from_disk=empty_queue,
        ),
        PlaintextWAL=SimpleNamespace(find_resendable=plaintext_query),
    ))


async def _clean_test_tasks(before: set[asyncio.Task[object]]) -> None:
    tasks = asyncio.all_tasks() - before
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["read", "write", "encrypt"])
@pytest.mark.parametrize("entry", ["api", "cli"])
async def test_requests_finish_before_the_client_closes(
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["read", "write", "encrypt"], entry: str,
) -> None:
    _dispatch_state(monkeypatch, kind)
    monkeypatch.setattr(
        network, "install_stats_counters", lambda connection: None,
    )
    client = _Client()
    started = asyncio.Event()
    socket_open_during_cleanup: list[bool] = []

    async def idle(connection: object) -> None:
        await asyncio.Event().wait()

    async def request(*args: object, **kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            socket_open_during_cleanup.append(not client.closed)
            client.released.set()

    monkeypatch.setattr(network, "provision_read_caps", idle)
    monkeypatch.setattr(network, "readables_to_mixwal", idle)
    monkeypatch.setattr(network, "drain_mixwal_read_single", request)
    monkeypatch.setattr(network, "drain_mixwal_write_single", request)
    monkeypatch.setattr(network, "start_resending", request)
    network.__mixnet_connected.set()
    before = asyncio.all_tasks()
    unrelated = asyncio.create_task(asyncio.Event().wait())
    bg = asyncio.create_task(
        network.start_background_threads(cast("ThinClient", client)),
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        stop = headless.stop if entry == "api" else _actions._shutdown
        await stop(bg, cast("ThinClient", client))
        assert bg.done()
        assert client.closed_after_release
        assert socket_open_during_cleanup == [True]
        assert not unrelated.done()
        assert not network._inflight_reads
        assert asyncio.all_tasks() == before | {unrelated}
    finally:
        network.shutdown()
        await _clean_test_tasks(before)


async def test_offline_shutdown_joins_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        network, "install_stats_counters", lambda connection: None,
    )
    client = _Client()
    started = asyncio.Event()

    async def provision(connection: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            client.released.set()

    monkeypatch.setattr(network, "provision_read_caps", provision)
    before = asyncio.all_tasks()
    bg = asyncio.create_task(
        network.start_background_threads(cast("ThinClient", client)),
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await headless.stop(bg, cast("ThinClient", client), timeout=0.02)
        assert bg.done()
        assert client.closed_after_release
        assert asyncio.all_tasks() == before
    finally:
        network.shutdown()
        await _clean_test_tasks(before)


@pytest.mark.parametrize("failure", [ValueError("bug"), TimeoutError("bug")])
async def test_worker_failure_propagates_after_closing(
    failure: Exception,
) -> None:
    client = _Client()

    async def fail() -> None:
        client.released.set()
        raise failure

    bg = asyncio.create_task(fail())
    with pytest.raises(type(failure)) as caught:
        await headless.stop(bg, cast("ThinClient", client))
    assert caught.value is failure
    assert client.closed_after_release


async def test_repeated_cancellation_waits_for_cleanup() -> None:
    client = _Client()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            client.released.set()

    bg = asyncio.create_task(worker())
    await started.wait()
    stopping = asyncio.create_task(
        headless.stop(bg, cast("ThinClient", client)),
    )
    try:
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.wait_for(cleaning.wait(), timeout=1)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not client.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert bg.done()
        assert client.closed_after_release
    finally:
        release.set()
        bg.cancel()
        stopping.cancel()
        await asyncio.gather(bg, stopping, return_exceptions=True)
