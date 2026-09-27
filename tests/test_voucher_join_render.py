from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from katzenqt import katzen

if TYPE_CHECKING:
    from katzenqt.qt_models import ConversationUIState


@pytest.mark.asyncio
async def test_every_added_member_is_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Item:
        def __init__(self, name: str) -> None:
            self.name = name

    rows: list[Item] = []
    infos: list[tuple[str, str]] = []

    def single_shot(delay: int, callback: Callable[[], None]) -> None:
        callback()

    async def run_in_io(coro: Coroutine[object, object, object]) -> None:
        coro.close()

    async def signal() -> None:
        return None

    monkeypatch.setattr(katzen, "ContactsItem", Item)
    monkeypatch.setattr(
        katzen, "QTimer", SimpleNamespace(singleShot=single_shot),
    )
    monkeypatch.setattr(
        katzen.network, "signal_readables_to_mixwal", signal,
    )
    monkeypatch.setattr(
        katzen.persistent, "Session", lambda engine: _NoRows(),
    )

    convo = SimpleNamespace(
        conversation_id=1, own_peer_id=None,
        contacts_standard_item=SimpleNamespace(appendRow=rows.append),
    )
    window = SimpleNamespace(
        iothread=SimpleNamespace(run_in_io=run_in_io),
        _info_plain=lambda title, text: infos.append((title, text)),
    )

    async def added(_conversation_id: int) -> list[str]:
        return ["alice", "bob"]

    window._wait_and_open_with_retries = added
    window._voucher_join_tasks = {}

    def run_join(c: "ConversationUIState") -> "Coroutine[object, object, None]":
        return katzen.MainWindow._run_voucher_join(
            cast("katzen.MainWindow", window), c,
        )

    window._run_voucher_join = run_join
    await katzen.MainWindow._await_voucher_join(
        cast("katzen.MainWindow", window),
        cast("ConversationUIState", convo),
    )

    assert [r.name for r in rows] == ["alice", "bob"]
    assert infos, "no join confirmation was shown"


class _NoRows:
    def __enter__(self) -> "_NoRows":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def exec(self, query: object) -> "_NoRows":
        return self

    def all(self) -> list[object]:
        return []
