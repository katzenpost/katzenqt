from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from katzenqt import network
from katzenqt.katzen import MainWindow, mixnet_status_text


def test_status_text_distinguishes_states() -> None:
    on_text, on_color = mixnet_status_text(True)
    off_text, off_color = mixnet_status_text(False)
    assert "connected" in on_text.lower()
    assert "offline" in off_text.lower()
    assert on_color != off_color


def test_listener_notified_and_accessor_tracks_state() -> None:
    seen: list[bool] = []
    listener = seen.append
    network.add_status_listener(listener)
    try:
        asyncio.run(
            network.on_connection_status({"is_connected": True, "err": None})
        )
        assert network.mixnet_connected() is True
        assert seen[-1] is True
        asyncio.run(
            network.on_connection_status({"is_connected": False, "err": None})
        )
        assert network.mixnet_connected() is False
        assert seen[-1] is False
    finally:
        network.remove_status_listener(listener)


class _FakeAction:
    def __init__(self, text: str) -> None:
        self.text = text
        self.enabled = True

    def setEnabled(self, value: bool) -> None:
        self.enabled = value


class _FakeMenu:
    def __init__(self) -> None:
        self.enabled = False
        self.actions: list[_FakeAction] = []

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def clear(self) -> None:
        self.actions = []

    def addAction(self, text: str) -> _FakeAction:
        action = _FakeAction(text)
        self.actions.append(action)
        return action


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.style = ""

    def setText(self, text: str) -> None:
        self.text = text

    def setStyleSheet(self, style: str) -> None:
        self.style = style


def test_render_wires_the_mixnet_status_menu() -> None:
    menu = _FakeMenu()
    window = cast(
        MainWindow,
        SimpleNamespace(
            mixnet_status_label=_FakeLabel(),
            ui=SimpleNamespace(menuMixnetStatus=menu),
        ),
    )

    MainWindow.render_mixnet_status(window, True)
    assert menu.enabled is True
    assert len(menu.actions) == 1
    assert "connected" in menu.actions[0].text.lower()
    assert menu.actions[0].enabled is False

    MainWindow.render_mixnet_status(window, False)
    assert len(menu.actions) == 1
    assert "offline" in menu.actions[0].text.lower()


def test_failing_listener_does_not_block_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[bool] = []

    def broken(connected: bool) -> None:
        raise RuntimeError("observer failed")

    network.add_status_listener(broken)
    network.add_status_listener(seen.append)
    try:
        network._notify_status(True)
        assert seen == [True]
        assert "connection status listener failed" in caplog.text
    finally:
        network.remove_status_listener(broken)
        network.remove_status_listener(seen.append)
