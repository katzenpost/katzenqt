"""Regression coverage for ``_qml_source_ready``.

Waiting for a QQuickWidget to load must not spin a nested Qt event loop
(``app.processEvents()``), which re-enters QtAsyncio's task stepping and raises
"Can not enter into task ... while another task ... is being executed".
"""
import asyncio
import os
from collections.abc import Callable
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtQuickWidgets import QQuickWidget  # noqa: E402

from katzenqt import katzen  # noqa: E402

_Slot = Callable[["QQuickWidget.Status"], None]


class _Signal:
    def __init__(self) -> None:
        self.slots: list[_Slot] = []

    def connect(self, slot: _Slot) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: _Slot) -> None:
        self.slots.remove(slot)

    def emit(self, value: "QQuickWidget.Status") -> None:
        for slot in list(self.slots):
            slot(value)


class _FakeQml:
    def __init__(self, status: "QQuickWidget.Status") -> None:
        self._status = status
        self.statusChanged = _Signal()

    def status(self) -> "QQuickWidget.Status":
        return self._status


@pytest.mark.asyncio
async def test_qml_source_ready_returns_when_already_settled() -> None:
    widget = _FakeQml(QQuickWidget.Status.Ready)
    await katzen._qml_source_ready(cast("QQuickWidget", widget))
    assert widget.statusChanged.slots == []  # never connected


@pytest.mark.asyncio
async def test_qml_source_ready_waits_for_the_status_signal() -> None:
    widget = _FakeQml(QQuickWidget.Status.Loading)
    task = asyncio.create_task(
        katzen._qml_source_ready(cast("QQuickWidget", widget)),
    )
    await asyncio.sleep(0)  # let it connect and await
    assert widget.statusChanged.slots  # connected
    widget.statusChanged.emit(QQuickWidget.Status.Ready)
    await asyncio.wait_for(task, timeout=1)
    assert widget.statusChanged.slots == []  # disconnected in finally


@pytest.mark.asyncio
async def test_qml_source_ready_settles_on_error_too() -> None:
    widget = _FakeQml(QQuickWidget.Status.Loading)
    task = asyncio.create_task(
        katzen._qml_source_ready(cast("QQuickWidget", widget)),
    )
    await asyncio.sleep(0)
    widget.statusChanged.emit(QQuickWidget.Status.Error)
    await asyncio.wait_for(task, timeout=1)
