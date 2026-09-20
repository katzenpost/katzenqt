from dataclasses import dataclass
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Self, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from katzenqt.headless import _actions

if TYPE_CHECKING:
    from katzenqt.models import GroupChatMessage


@dataclass
class _Message:
    membership_hash: bytes = b""


@dataclass(frozen=True)
class _Conversation:
    id: int = 1
    write_cap: UUID = UUID(int=1)


@dataclass(frozen=True)
class _Pending:
    id: int = 7


@dataclass(frozen=True)
class _Result:
    value: object

    def first(self) -> object:
        return self.value


@dataclass
class _Clock:
    now: float = 0.0

    def time(self) -> float:
        return self.now

    async def advance(self, delay: float) -> None:
        self.now += 1


class _Query:
    def where(self, predicate: object) -> Self:
        return self


class _SendOperation:
    def __init__(
        self, *, bacap_stream: UUID, messages: list[object],
    ) -> None:
        pass

    def serialize(
        self, *, chunk_size: int, conversation_id: int,
    ) -> tuple[list[UUID], list[_Pending]]:
        return [], [_Pending()]

    async def serialize_async(
        self, *, chunk_size: int, conversation_id: int,
    ) -> tuple[list[UUID], list[_Pending]]:
        return self.serialize(
            chunk_size=chunk_size, conversation_id=conversation_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_send_starts_and_closes_exactly_one_session(
    monkeypatch: pytest.MonkeyPatch, acknowledged: bool,
) -> None:
    calls = 0

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None,
            exc: BaseException | None, traceback: TracebackType | None,
        ) -> bool:
            return False

        async def exec(self, statement: _Query) -> _Result:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Result(_Conversation())
            return _Result(object() if acknowledged else None)

        def add(self, row: object) -> None:
            pass

        async def commit(self) -> None:
            pass

    def select(*entities: object) -> _Query:
        return _Query()

    monkeypatch.setattr(_actions, "select", select)
    monkeypatch.setattr(_actions, "persistent", SimpleNamespace(
        asession=Session, Conversation=SimpleNamespace(name="demo"),
        PlaintextWAL=_Pending, SentLog=SimpleNamespace(id=7),
    ))
    monkeypatch.setattr(_actions, "models", SimpleNamespace(
        SendOperation=_SendOperation,
    ))
    monkeypatch.setattr(_actions, "conversation_handlers", SimpleNamespace(
        local_membership_hash=AsyncMock(return_value=b"m" * 32),
    ))
    monkeypatch.setattr(_actions, "network", SimpleNamespace(
        check_for_new=AsyncMock(),
    ))
    connection, background = object(), object()
    connect = AsyncMock(return_value=(connection, background))
    close = AsyncMock()
    monkeypatch.setattr(_actions, "_connect_and_start", connect)
    monkeypatch.setattr(_actions, "_shutdown", close)
    clock = _Clock()
    monkeypatch.setattr(_actions, "asyncio", SimpleNamespace(
        get_event_loop=lambda: clock, sleep=clock.advance,
    ))
    code = await _actions._send_one_gcm(
        "demo", cast("GroupChatMessage", _Message()), timeout=1,
    )
    assert code == (0 if acknowledged else 3)
    connect.assert_awaited_once_with()
    close.assert_awaited_once_with(background, connection)
