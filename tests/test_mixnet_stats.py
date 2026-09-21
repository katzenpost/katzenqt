"""Counters behind the Mixnet-status "Stats" window.

Every pigeonhole read/write -- normal streams, substreams, and voucher streams
-- is counted by wrapping the single shared connection's
encrypt_read/encrypt_write/start_resending_encrypted_message.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenpost_thinclient import (  # noqa: E402
    BoxIDNotFoundError,
    ThinClientOfflineError,
)

from katzenqt import katzen, network  # noqa: E402


class _StubConnection:
    """Minimal stand-in for the ThinClient surface the wrappers touch."""

    def __init__(self, *, read_plaintext: bytes = b"", boxnotfound: bool = False,
                 link_down: bool = False, gate=None):
        self.read_plaintext = read_plaintext
        self.boxnotfound = boxnotfound
        self.link_down = link_down
        self.gate = gate
        self.calls: "list[tuple[str, dict]]" = []

    async def encrypt_read(self, **kwargs):
        self.calls.append(("encrypt_read", kwargs))
        return SimpleNamespace(read_cap=b"r")

    async def encrypt_write(self, **kwargs):
        self.calls.append(("encrypt_write", kwargs))
        return SimpleNamespace(write_cap=b"w")

    async def start_resending_encrypted_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        if self.gate is not None:
            await self.gate.wait()
        if self.link_down:
            raise ThinClientOfflineError()
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
    assert snap["packets_sent"] == 2
    assert snap["packets_in_flight"] == 0
    assert snap["packets_timed_out"] == 0
    assert snap["packets_link_down"] == 0
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


@pytest.mark.asyncio
async def test_packets_in_flight_gauge_tracks_a_pending_send():
    network.reset_stats()
    gate = asyncio.Event()
    conn = _StubConnection(gate=gate)
    network.install_stats_counters(conn)
    task = asyncio.create_task(
        conn.start_resending_encrypted_message(read_cap=b"r", write_cap=None)
    )
    for _ in range(100):
        await asyncio.sleep(0)
        if network.stats_snapshot()["packets_in_flight"] == 1:
            break
    assert network.stats_snapshot()["packets_in_flight"] == 1
    gate.set()
    await task
    assert network.stats_snapshot()["packets_in_flight"] == 0


@pytest.mark.asyncio
async def test_link_down_is_counted_and_reraised():
    network.reset_stats()
    conn = _StubConnection(link_down=True)
    network.install_stats_counters(conn)
    with pytest.raises(ThinClientOfflineError):
        await conn.start_resending_encrypted_message(read_cap=b"r", write_cap=None)
    snap = network.stats_snapshot()
    assert snap["packets_sent"] == 1
    assert snap["packets_link_down"] == 1
    assert snap["packets_timed_out"] == 0
    assert snap["packets_in_flight"] == 0


@pytest.mark.asyncio
async def test_lost_race_counts_a_timeout_only_when_asked():
    async def _hang():
        await asyncio.Event().wait()

    async def _run(*, count_timeout: bool) -> None:
        with pytest.raises(network.ConnectionLifeInterruptedError):
            await network._rpc_racing_connection_life(
                bacap_uuid="test",
                what="test",
                rpc_factory=_hang,
                backstop_s=0.01,
                reconnect_marker=asyncio.Event(),
                epoch_marker=asyncio.Event(),
                count_timeout=count_timeout,
            )

    network.reset_stats()
    await _run(count_timeout=True)
    assert network.stats_snapshot()["packets_timed_out"] == 1
    await _run(count_timeout=False)
    assert network.stats_snapshot()["packets_timed_out"] == 1


def test_stats_dialog_shows_the_snapshot_and_timeout_percentage():
    app = QApplication.instance() or QApplication([])
    network.reset_stats()
    network.stats.reads_sent = 1234
    network.stats.packets_sent = 2000
    network.stats.packets_timed_out = 10
    dialog = katzen.StatsDialog(None)
    assert dialog._labels["reads_sent"].text() == "1,234"
    assert dialog._labels["writes_acked"].text() == "0"
    assert dialog._labels["packets_sent"].text() == "2,000"
    assert dialog._labels["packets_timed_out"].text() == "10 (0.5%)"
    dialog.deleteLater()
    _ = app


def test_stats_dialog_timeout_percentage_with_no_sends():
    app = QApplication.instance() or QApplication([])
    network.reset_stats()
    dialog = katzen.StatsDialog(None)
    assert dialog._labels["packets_timed_out"].text() == "0 (0.0%)"
    dialog.deleteLater()
    _ = app
