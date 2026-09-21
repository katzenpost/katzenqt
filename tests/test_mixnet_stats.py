"""Counters behind the Mixnet-status "Stats" window.

Every pigeonhole read/write -- normal streams, substreams, and voucher streams
-- is counted by wrapping the single shared connection's
encrypt_read/encrypt_write/start_resending_encrypted_message.
"""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenpost_thinclient import BoxIDNotFoundError  # noqa: E402

from katzenqt import katzen, network  # noqa: E402


class _StubConnection:
    """Minimal stand-in for the ThinClient surface the wrappers touch."""

    def __init__(self, *, read_plaintext: bytes = b"", boxnotfound: bool = False):
        self.read_plaintext = read_plaintext
        self.boxnotfound = boxnotfound
        self.calls: "list[tuple[str, dict]]" = []

    async def encrypt_read(self, **kwargs):
        self.calls.append(("encrypt_read", kwargs))
        return SimpleNamespace(read_cap=b"r")

    async def encrypt_write(self, **kwargs):
        self.calls.append(("encrypt_write", kwargs))
        return SimpleNamespace(write_cap=b"w")

    async def start_resending_encrypted_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        if self.boxnotfound:
            raise BoxIDNotFoundError()
        if kwargs.get("write_cap") is not None:
            return SimpleNamespace(plaintext=b"")
        return SimpleNamespace(plaintext=self.read_plaintext)


@pytest.mark.asyncio
async def test_counters_cover_prepared_sent_payload_ack_and_boxnotfound():
    network.reset_stats()
    conn = _StubConnection()
    network.install_stats_counters(conn)

    await conn.encrypt_read(read_cap=b"r")
    await conn.encrypt_write(write_cap=b"w", plaintext=b"x")
    await conn.start_resending_encrypted_message(read_cap=b"r", write_cap=None)
    await conn.start_resending_encrypted_message(read_cap=None, write_cap=b"w")

    snap = network.stats_snapshot()
    assert snap["reads_prepared"] == 1
    assert snap["writes_prepared"] == 1
    assert snap["reads_sent"] == 1
    assert snap["writes_sent"] == 1
    assert snap["reads_with_payload"] == 0  # stub read plaintext is empty
    assert snap["reads_boxnotfound"] == 0
    assert snap["writes_acked"] == 1


@pytest.mark.asyncio
async def test_read_with_payload_is_counted():
    network.reset_stats()
    conn = _StubConnection(read_plaintext=b"hello")
    network.install_stats_counters(conn)
    await conn.start_resending_encrypted_message(read_cap=b"r", write_cap=None)
    snap = network.stats_snapshot()
    assert snap["reads_sent"] == 1
    assert snap["reads_with_payload"] == 1


@pytest.mark.asyncio
async def test_boxnotfound_is_counted_and_reraised():
    network.reset_stats()
    conn = _StubConnection(boxnotfound=True)
    network.install_stats_counters(conn)
    with pytest.raises(BoxIDNotFoundError):
        await conn.start_resending_encrypted_message(
            read_cap=b"r", write_cap=None,
        )
    snap = network.stats_snapshot()
    assert snap["reads_sent"] == 1
    assert snap["reads_boxnotfound"] == 1
    assert snap["reads_with_payload"] == 0


@pytest.mark.asyncio
async def test_a_write_boxnotfound_is_not_a_read_boxnotfound():
    network.reset_stats()
    conn = _StubConnection(boxnotfound=True)
    network.install_stats_counters(conn)
    # A write send is classified by write_cap; BoxIDNotFound on it must not
    # land in the read outcome counter.
    with pytest.raises(BoxIDNotFoundError):
        await conn.start_resending_encrypted_message(
            read_cap=None, write_cap=b"w",
        )
    snap = network.stats_snapshot()
    assert snap["writes_sent"] == 1
    assert snap["reads_boxnotfound"] == 0


@pytest.mark.asyncio
async def test_install_is_idempotent():
    network.reset_stats()
    conn = _StubConnection()
    network.install_stats_counters(conn)
    network.install_stats_counters(conn)
    await conn.start_resending_encrypted_message(read_cap=b"r", write_cap=None)
    assert network.stats_snapshot()["reads_sent"] == 1


def test_stats_dialog_shows_the_snapshot():
    app = QApplication.instance() or QApplication([])
    network.reset_stats()
    network.stats.reads_sent = 1234
    dialog = katzen.StatsDialog(None)
    assert dialog._labels["reads_sent"].text() == "1,234"
    assert dialog._labels["writes_acked"].text() == "0"
    dialog.deleteLater()
    _ = app
