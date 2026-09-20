from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from katzenqt import katzen


@pytest.mark.asyncio
async def test_restarted_voucher_join_keeps_its_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(conversation_id=11)
    second = SimpleNamespace(conversation_id=22)
    factories: dict[str, Callable[[], Coroutine[object, object, None]]] = {}
    seen: list[int] = []

    def supervise(
        name: str, factory: Callable[[], Coroutine[object, object, None]],
    ) -> None:
        factories[name] = factory

    async def join(state: SimpleNamespace) -> None:
        seen.append(state.conversation_id)

    window = SimpleNamespace(
        conversation_state_by_id={11: first, 22: second},
        _await_voucher_join=join, _supervised_listener=supervise,
    )
    monkeypatch.setattr(
        katzen, "pending_joiner_join_conversation_ids",
        Mock(return_value=[11, 22]),
    )
    await katzen._resume_pending_joins(cast(katzen.MainWindow, window))
    await factories["_await_voucher_join:11"]()
    await factories["_await_voucher_join:22"]()
    await factories["_await_voucher_join:11"]()
    assert seen == [11, 22, 11]
