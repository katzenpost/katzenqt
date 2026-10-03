"""Network consensus summary: recovering the epoch duration, decoding the PKI
topology, and rendering the modeless dialog."""
import os
from datetime import datetime, timezone

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import katzen, network  # noqa: E402


def _blob(desc: "dict[str, object]") -> bytes:
    return cbor2.dumps(desc)


_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
_PERIOD = 120
_EPOCH = int(
    (_NOW - network.KATZENPOST_EPOCH_ORIGIN).total_seconds() // _PERIOD
)


def _sample_doc() -> "dict[str, object]":
    return {
        "Epoch": _EPOCH,
        "GenesisEpoch": 1000,
        "Topology": [
            [_blob({"Name": "mix1", "Addresses": {"tcp4": ["tcp://10.89.0.12:1021"]}})],
            [_blob({"Name": "mix2", "Addresses": {"tcp4": ["tcp://10.89.0.6:1024"]}})],
        ],
        "GatewayNodes": [
            _blob({"Name": "gw1", "Addresses": {"tcp4": ["tcp://10.89.0.11:1006"]}}),
        ],
        "ServiceNodes": [],
        # Storage replicas arrive already decoded as maps, not CBOR blobs.
        "StorageReplicas": [
            {"Name": "rep1", "Addresses": {"tcp": ["tcp://replica1:1036"]}},
        ],
    }


def test_derive_epoch_period_from_the_fixed_origin() -> None:
    assert network.derive_epoch_period_seconds(_EPOCH, now=_NOW) == _PERIOD


def test_derive_epoch_period_handles_missing_or_zero_epoch() -> None:
    assert network.derive_epoch_period_seconds(None, now=_NOW) is None
    assert network.derive_epoch_period_seconds(0, now=_NOW) is None


def test_summarize_pki_document_decodes_topology_and_consensus() -> None:
    summary = network.summarize_pki_document(_sample_doc(), now=_NOW)
    assert summary is not None
    assert summary.epoch == _EPOCH
    assert summary.genesis_epoch == 1000
    assert summary.period_seconds == _PERIOD
    assert summary.epochs_elapsed == _EPOCH - 1000
    assert summary.consensus_seconds == (_EPOCH - 1000) * _PERIOD
    assert summary.mix_layers[0][0].name == "mix1"
    assert summary.mix_layers[0][0].addresses == ["tcp://10.89.0.12:1021"]
    assert summary.mix_layers[1][0].name == "mix2"
    assert summary.gateways[0].name == "gw1"
    assert summary.gateways[0].addresses == ["tcp://10.89.0.11:1006"]
    assert summary.service_nodes == []
    assert summary.storage_replicas[0].name == "rep1"
    assert summary.storage_replicas[0].addresses == ["tcp://replica1:1036"]


def test_node_address_without_a_scheme_gets_the_transport_prefix() -> None:
    doc = {
        "GatewayNodes": [
            _blob({"Name": "gw", "Addresses": {"tcp": ["gw:1234"]}}),
        ],
    }
    summary = network.summarize_pki_document(doc, now=_NOW)
    assert summary is not None
    assert summary.gateways[0].addresses == ["tcp://gw:1234"]


def test_summarize_pki_document_is_none_without_a_document() -> None:
    assert network.summarize_pki_document(None) is None
    assert network.summarize_pki_document({}) is None


def test_summarize_tolerates_undecodable_node_blobs() -> None:
    doc = _sample_doc()
    doc["GatewayNodes"] = [b"not cbor", _blob({"Name": "gw2"})]
    summary = network.summarize_pki_document(doc, now=_NOW)
    assert summary is not None
    assert [n.name for n in summary.gateways] == ["gw2"]


def test_format_duration() -> None:
    assert network.format_duration(None) == "unknown"
    assert network.format_duration(0) == "0s"
    assert network.format_duration(90) == "1m 30s"
    assert network.format_duration(3700) == "1h 1m"
    assert network.format_duration(2 * 86400) == "2d 0h"
    assert network.format_duration(400 * 86400) == "1y 35d"


@pytest.mark.asyncio
async def test_consensus_dialog_renders_the_summary_and_tree() -> None:
    app = QApplication.instance() or QApplication([])
    doc = _sample_doc()

    async def fetch() -> "dict[str, object]":
        return doc

    dialog = katzen.ConsensusDialog(None, fetch)
    await dialog._refresh_async()

    assert dialog._fields["epoch"].text() == str(_EPOCH)
    assert dialog._fields["genesis"].text() == "1000"
    assert dialog._fields["epochs"].text() == str(_EPOCH - 1000)
    assert dialog._fields["period"].text() != "—"
    assert dialog._fields["consensus"].text() != "—"
    # Gateways first, then two mix layers, service nodes, storage replicas.
    assert dialog._tree.topLevelItemCount() == 5
    gateways = dialog._tree.topLevelItem(0)
    layer1 = dialog._tree.topLevelItem(1)
    layer2 = dialog._tree.topLevelItem(2)
    replicas = dialog._tree.topLevelItem(4)
    assert gateways is not None
    assert layer1 is not None
    assert layer2 is not None
    assert replicas is not None
    gateway0 = gateways.child(0)
    mix1 = layer1.child(0)
    mix2 = layer2.child(0)
    replica0 = replicas.child(0)
    assert gateway0 is not None
    assert mix1 is not None
    assert mix2 is not None
    assert replica0 is not None
    assert gateways.text(0) == "Gateways"
    assert gateway0.text(0) == "gw1"
    assert mix1.text(0) == "mix1"
    assert mix2.text(0) == "mix2"
    assert replicas.text(0) == "Storage replicas"
    assert replica0.text(0) == "rep1"
    # Selecting a cell must paint the highlight behind the text.
    style = dialog._tree.styleSheet()
    assert "background-color" in style and "color" in style
    dialog.deleteLater()
    _ = app


@pytest.mark.asyncio
async def test_consensus_dialog_handles_no_document() -> None:
    app = QApplication.instance() or QApplication([])

    async def fetch() -> None:
        return None

    dialog = katzen.ConsensusDialog(None, fetch)
    await dialog._refresh_async()
    assert dialog._fields["epoch"].text() == "no PKI document yet"
    dialog.deleteLater()
    _ = app


@pytest.mark.asyncio
async def test_consensus_dialog_survives_a_failing_fetch() -> None:
    """A daemon-down fetch must not raise out of the timer's task."""
    app = QApplication.instance() or QApplication([])

    async def fetch() -> None:
        raise ConnectionError("daemon down")

    dialog = katzen.ConsensusDialog(None, fetch)
    await dialog._refresh_async()
    assert dialog._fields["epoch"].text() == "PKI document unavailable"
    dialog.deleteLater()
    _ = app


@pytest.mark.asyncio
async def test_get_pki_document_is_none_before_connect() -> None:
    assert await network.get_pki_document(None) is None
