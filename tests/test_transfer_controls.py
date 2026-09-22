from collections.abc import Coroutine
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock

import pytest
from PySide6.QtCore import Qt
from sqlmodel import select

from katzenqt import katzen, network, persistent, qt_models
from katzenqt.qt_models import DownloadsModel


def test_resuming_clears_the_failed_display() -> None:
    model = DownloadsModel()
    stream = UUID(int=1)
    model.start_transfer(stream, 1, "bob", 3)
    model.fail_transfer(stream, "missing box")
    model.set_paused(stream, paused=False)
    index = model.index(0, 2)
    assert model.data(index, qt_models.ROLE_TRANSFER_FAILED) is False
    assert model.data(index, qt_models.ROLE_TRANSFER_FAILURE_REASON) is None
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "Downloading"


@pytest.mark.asyncio
async def test_failed_transfer_menu_removes_the_clicked_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = DownloadsModel()
    stream = UUID(int=2)
    model.start_transfer(stream, 1, "bob", 3)
    model.fail_transfer(stream, "missing box")
    offered: list[str] = []

    class Menu:
        def __init__(self, parent: object) -> None:
            pass

        def addAction(self, name: str) -> str:
            offered.append(name)
            return name

    async def chosen(menu: Menu, pos: object) -> str:
        return "Remove"

    async def run(coroutine: Coroutine[object, object, None]) -> None:
        await coroutine

    remove = AsyncMock()
    monkeypatch.setattr(network, "dismiss_failed_transfer", remove)
    monkeypatch.setattr(katzen, "QMenu", Menu)
    monkeypatch.setattr(katzen, "_menu_chosen", chosen)
    view = SimpleNamespace(
        indexAt=lambda pos: model.index(0, 0), model=lambda: model,
        viewport=lambda: SimpleNamespace(mapToGlobal=lambda pos: pos),
    )
    window = SimpleNamespace(
        transfers_view=view, iothread=SimpleNamespace(run_in_io=run),
    )
    await katzen.MainWindow.transfers_context_menu(window, None)
    assert offered == ["Remove"]
    remove.assert_awaited_once_with(bacap_stream=stream)
    assert model.rowCount() == 0


@pytest.mark.asyncio
async def test_dismissal_removes_persisted_failed_pieces() -> None:
    stream = UUID(int=3)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(
            id=stream, read_cap=b"r" * 136, substream_failure="bad frame",
        ))
        sess.add(persistent.ConversationPeer(
            name=":substream:999:test", read_cap_id=stream, active=False,
        ))
        sess.add(persistent.ReceivedPiece(
            read_cap=stream, bacap_index=b"i" * 8,
            chunk_type=b"C", chunk=b"data",
        ))
        await sess.commit()
    await network.dismiss_failed_transfer(bacap_stream=stream)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, stream)
        assert rcw is not None and rcw.substream_failure is None
        rows = (await sess.exec(select(persistent.ReceivedPiece))).all()
        assert not rows
