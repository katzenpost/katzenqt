import asyncio

from katzenqt import network
from katzenqt.katzen import mixnet_status_text


def test_status_text_distinguishes_states():
    on_text, on_color = mixnet_status_text(True)
    off_text, off_color = mixnet_status_text(False)
    assert "connected" in on_text.lower()
    assert "offline" in off_text.lower()
    assert on_color != off_color


def test_listener_notified_and_accessor_tracks_state():
    seen = []
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
        getattr(network, "__status_listeners").remove(listener)


def test_render_wires_the_mixnet_status_menu():
    import types
    from katzenqt.katzen import MainWindow

    class FakeAction:
        def __init__(self, text):
            self.text = text
            self.enabled = True

        def setEnabled(self, v):
            self.enabled = v

    class FakeMenu:
        def __init__(self):
            self.enabled = False
            self.actions = []

        def setEnabled(self, v):
            self.enabled = v

        def clear(self):
            self.actions = []

        def addAction(self, text):
            a = FakeAction(text)
            self.actions.append(a)
            return a

    class FakeLabel:
        def setText(self, t):
            self.text = t

        def setStyleSheet(self, s):
            self.style = s

    win = MainWindow.__new__(MainWindow)
    win.mixnet_status_label = FakeLabel()
    menu = FakeMenu()
    win.ui = types.SimpleNamespace(menuMixnetStatus=menu)

    MainWindow.render_mixnet_status(win, True)
    assert menu.enabled is True
    assert len(menu.actions) == 1
    assert "connected" in menu.actions[0].text.lower()
    assert menu.actions[0].enabled is False

    MainWindow.render_mixnet_status(win, False)
    assert len(menu.actions) == 1
    assert "offline" in menu.actions[0].text.lower()
