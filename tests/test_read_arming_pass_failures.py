import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from katzenqt import network

if TYPE_CHECKING:
    from katzenqt._thinclient import ThinClient

pytestmark = pytest.mark.asyncio


@dataclass
class _LoopState:
    pass_no: int = 0
    published: list[int] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    failed: bool = False

    def publish(self) -> None:
        self.published.append(self.pass_no)


@dataclass(frozen=True)
class _ReadCap:
    id: UUID = UUID(int=1)
    read_cap: bytes = b"cap"
    next_index: bytes = b"index000"


@dataclass(frozen=True)
class _Peer:
    name: str = "bob"


@dataclass(frozen=True)
class _Rows:
    rows: list[tuple[_Peer, _ReadCap]]

    def all(self) -> list[tuple[_Peer, _ReadCap]]:
        return self.rows


class _Query:
    def where(self, predicate: object) -> Self:
        return self


class _Field:
    def not_in(self, values: object) -> bool:
        return True


class _MixWAL:
    bacap_stream = UUID(int=1)

    def __init__(
        self, *, bacap_stream: UUID, plaintextwal: None,
        envelope_hash: bytes, encrypted_payload: bytes,
        envelope_descriptor: bytes, next_message_index: bytes,
        current_message_index: bytes, is_read: bool,
    ) -> None:
        self.bacap_stream = bacap_stream


def _busy() -> OperationalError:
    return OperationalError("select", {}, Exception("database is locked"))


def _install_loop(
    monkeypatch: pytest.MonkeyPatch, *, failure: Exception,
    stage: str, prior_success: bool = False,
) -> tuple[_LoopState, "ThinClient"]:
    quit_event = asyncio.Event()
    populated = asyncio.Event()
    populated.set()
    arming = asyncio.Event()
    arming.set()
    state = _LoopState()

    def maybe_fail(where: str) -> None:
        if where == stage and state.pass_no == (2 if prior_success else 1):
            state.failed = True
            raise failure

    class Session:
        async def __aenter__(self) -> Self:
            state.pass_no += 1
            if state.pass_no > 3:
                raise AssertionError("retry loop did not terminate")
            maybe_fail("enter")
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None,
            exc: BaseException | None, traceback: TracebackType | None,
        ) -> bool:
            return False

        async def exec(self, statement: object) -> _Rows:
            maybe_fail("query")
            rows = (
                [(_Peer(), _ReadCap())]
                if prior_success and state.pass_no == 1 else []
            )
            return _Rows(rows)

        def add(self, row: _MixWAL) -> None:
            pass

        async def commit(self) -> None:
            maybe_fail("commit")
            if state.failed:
                quit_event.set()
            arming.set()

    async def sleep(seconds: float) -> None:
        state.sleeps.append(seconds)
        await asyncio.sleep(0)

    def select(*entities: object) -> _Query:
        return _Query()

    monkeypatch.setattr(network, "asyncio", SimpleNamespace(
        sleep=sleep, wait_for=asyncio.wait_for,
    ))
    monkeypatch.setattr(network, "OperationalError", OperationalError)
    monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.001)
    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network, "__resend_queue_populated", populated)
    monkeypatch.setattr(
        network, "__mixwal_updated", SimpleNamespace(set=state.publish),
    )
    monkeypatch.setattr(network, "readables_to_mixwal_event", arming)
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(network, "select", select)
    field = _Field()
    monkeypatch.setattr(network, "persistent", SimpleNamespace(
        asession=Session, MixWAL=_MixWAL,
        ConversationPeer=SimpleNamespace(active=True, read_cap_id=field),
        ReadCapWAL=SimpleNamespace(id=field, paused=False),
    ))
    reply = SimpleNamespace(
        envelope_hash=b"h", message_ciphertext=b"c",
        envelope_descriptor=b"d", next_message_box_index=b"next",
    )
    connection = SimpleNamespace(
        encrypt_read=AsyncMock(return_value=reply),
    )
    return state, cast("ThinClient", connection)


@pytest.mark.parametrize("stage", ["enter", "query", "commit"])
@pytest.mark.parametrize("prior_success", [False, True])
async def test_busy_pass_retries_without_unbound_or_stale_rows(
    monkeypatch: pytest.MonkeyPatch, stage: str, prior_success: bool,
) -> None:
    state, connection = _install_loop(
        monkeypatch, failure=_busy(), stage=stage,
        prior_success=prior_success,
    )
    await network.readables_to_mixwal(connection)
    assert state.pass_no == (3 if prior_success else 2)
    assert state.published == ([1] if prior_success else [])
    assert 5 in state.sleeps


@pytest.mark.parametrize("failure", [
    ValueError("programming error"),
    OperationalError("select", {}, Exception("no such table")),
    IntegrityError("commit", {}, Exception("invariant broken")),
])
async def test_permanent_pass_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    state, connection = _install_loop(
        monkeypatch, failure=failure, stage="query",
    )
    with pytest.raises(type(failure)) as caught:
        await network.readables_to_mixwal(connection)
    assert caught.value is failure
    assert state.pass_no == 1
    assert not state.published


async def test_supervisor_restarts_after_an_unexpected_pass_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quit_event = asyncio.Event()
    calls: list[int] = []

    async def flaky(connection: object) -> None:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("invariant bug")
        quit_event.set()

    async def immediate_wait(*, idle_retry_s: float = 0.0) -> bool:
        return True

    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network, "readables_to_mixwal", flaky)
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown", immediate_wait,
    )
    await network.readables_to_mixwal_supervised(object())
    assert len(calls) == 2



@pytest.mark.parametrize("stage", ["query", "commit"])
async def test_duplicate_arming_retries_instead_of_killing_the_loop(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    failure = IntegrityError(
        "INSERT INTO mixwal", {},
        Exception("UNIQUE constraint failed: mixwal.bacap_stream"),
    )
    state, connection = _install_loop(
        monkeypatch, failure=failure, stage=stage,
    )
    await network.readables_to_mixwal(connection)
    assert state.pass_no == 2
    assert 5 in state.sleeps


def _install_arming_rpc_failure(
    monkeypatch: pytest.MonkeyPatch, *, waits: int,
) -> tuple[_LoopState, "ThinClient"]:
    """A loop whose per-peer arming RPC always fails, so every pass ends in
    the retry_needed wait. Quits once ``waits`` waits have been recorded."""
    quit_event = asyncio.Event()
    populated = asyncio.Event()
    populated.set()
    arming = asyncio.Event()
    arming.set()
    state = _LoopState()

    class Session:
        async def __aenter__(self) -> Self:
            state.pass_no += 1
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None,
            exc: BaseException | None, traceback: TracebackType | None,
        ) -> bool:
            return False

        async def exec(self, statement: object) -> _Rows:
            return _Rows([(_Peer(), _ReadCap())])

        def add(self, row: _MixWAL) -> None:
            pass

        async def commit(self) -> None:
            pass

    async def sleep(seconds: float) -> None:
        state.sleeps.append(seconds)
        if len(state.sleeps) >= waits:
            quit_event.set()
        await asyncio.sleep(0)

    def select(*entities: object) -> _Query:
        return _Query()

    monkeypatch.setattr(network, "asyncio", SimpleNamespace(
        sleep=sleep, wait_for=asyncio.wait_for,
    ))
    monkeypatch.setattr(network, "_ARMING_SWEEP_S", 0.001)
    monkeypatch.setattr(network, "__should_quit", quit_event)
    monkeypatch.setattr(network, "__resend_queue_populated", populated)
    monkeypatch.setattr(
        network, "__mixwal_updated", SimpleNamespace(set=state.publish),
    )
    monkeypatch.setattr(network, "readables_to_mixwal_event", arming)
    monkeypatch.setattr(
        network, "_wait_for_connection_or_shutdown",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(network, "select", select)
    field = _Field()
    monkeypatch.setattr(network, "persistent", SimpleNamespace(
        asession=Session, MixWAL=_MixWAL,
        ConversationPeer=SimpleNamespace(active=True, read_cap_id=field),
        ReadCapWAL=SimpleNamespace(id=field, paused=False),
    ))
    connection = SimpleNamespace(
        encrypt_read=AsyncMock(side_effect=RuntimeError("daemon said no")),
    )
    return state, cast("ThinClient", connection)


async def test_a_failing_arming_pass_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arming RPC is local to the daemon, so a pass that fails on every
    peer can come straight back. Waiting a flat five seconds each time is
    both too long after one hiccup and too short under a lasting failure."""
    state, connection = _install_arming_rpc_failure(monkeypatch, waits=3)
    await network.readables_to_mixwal(connection)
    assert len(state.sleeps) == 3
    assert max(state.sleeps) < 5
    assert state.sleeps == sorted(state.sleeps)
    assert state.sleeps[-1] > state.sleeps[0]
