from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from katzenqt import katzen


@pytest.mark.asyncio
async def test_induction_failure_dialog_reports_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs: list[tuple[str, str]] = []

    class Dialog:
        def __init__(self, parent: object) -> None:
            pass

        def setWindowTitle(self, title: str) -> None:
            pass

        def setLabelText(self, text: str) -> None:
            pass

        def textValue(self) -> str:
            return "aaaa"

    async def finished(dialog: object) -> bool:
        return True

    deferred: list[Any] = []

    def single_shot(delay: int, callback: Any) -> None:
        deferred.append(callback)

    def critical(parent: object, title: str, text: str) -> None:
        dialogs.append((title, text))

    monkeypatch.setattr(katzen, "QInputDialog", Dialog)
    monkeypatch.setattr(katzen, "_dialog_finished", finished)
    monkeypatch.setattr(
        katzen, "QTimer", SimpleNamespace(singleShot=single_shot),
    )
    monkeypatch.setattr(
        katzen, "QMessageBox", SimpleNamespace(critical=critical),
    )

    async def run_in_io(coro: Coroutine[Any, Any, Any]) -> None:
        coro.close()
        raise RuntimeError("induction exploded")

    window = SimpleNamespace(
        convo_state=lambda: SimpleNamespace(conversation_id=1),
        iothread=SimpleNamespace(run_in_io=run_in_io, kp_client=object()),
    )
    await katzen.MainWindow.induct_via_voucher(window)
    assert deferred, "no dialog was scheduled"
    for callback in deferred:
        callback()
    assert dialogs, "no failure dialog was shown"
    assert any("induction exploded" in text for _, text in dialogs)
