"""Network consensus summary: recovering the epoch duration, decoding the PKI
topology, and rendering the modeless dialog."""
import os
from datetime import datetime, timezone

import cbor2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from katzenqt import katzen, network  # noqa: E402


def _blob(desc: dict) -> bytes:
    return cbor2.dumps(desc)


_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
_PERIOD = 120
_EPOCH = int(
    (_NOW - network.KATZENPOST_EPOCH_ORIGIN).total_seconds() // _PERIOD
)


def _sample_doc() -> dict:
    return {
        "Epoch": _EPOCH,
        "GenesisEpoch": 1000,
        "Topology": [
            [_blob({"Name": "mix1", "Addresses": {"tcp": ["mix1:1000"]}})],
            [_blob({"Name": "mix2", "Addresses": {"tcp": ["mix2:1000"]}})],
        ],
        "GatewayNodes": [
            _blob({"Name": "gw1", "Addresses": {"tcp": ["gw1:1006"]}}),
        ],
        "ServiceNodes": [],
        "StorageReplicas": [
            _blob({"Name": "rep1", "Addresses": {"tcp": ["rep1:2000"]}}),
        ],
    }


def test_derive_epoch_period_from_the_fixed_origin():
    assert network.derive_epoch_period_seconds(_EPOCH, now=_NOW) == _PERIOD


def test_derive_epoch_period_handles_missing_or_zero_epoch():
    assert network.derive_epoch_period_seconds(None, now=_NOW) is None
    assert network.derive_epoch_period_seconds(0, now=_NOW) is None


def test_summarize_pki_document_decodes_topology_and_consensus():
    summary = network.summarize_pki_document(_sample_doc(), now=_NOW)
    assert summary is not None
    assert summary.epoch == _EPOCH
    assert summary.genesis_epoch == 1000
    assert summary.period_seconds == _PERIOD
    assert summary.epochs_elapsed == _EPOCH - 1000
    assert summary.consensus_seconds == (_EPOCH - 1000) * _PERIOD
    assert summary.mix_layers[0][0].name == "mix1"
    assert summary.mix_layers[0][0].addresses == ["tcp://mix1:1000"]
    assert summary.mix_layers[1][0].name == "mix2"
    assert summary.gateways[0].name == "gw1"
    assert summary.gateways[0].addresses == ["tcp://gw1:1006"]
    assert summary.service_nodes == []
    assert summary.storage_replicas[0].name == "rep1"


def test_summarize_pki_document_is_none_without_a_document():
    assert network.summarize_pki_document(None) is None
    assert network.summarize_pki_document({}) is None


def test_summarize_tolerates_undecodable_node_blobs():
    doc = _sample_doc()
    doc["GatewayNodes"] = [b"not cbor", _blob({"Name": "gw2"})]
    summary = network.summarize_pki_document(doc, now=_NOW)
    assert [n.name for n in summary.gateways] == ["gw2"]


def test_format_duration():
    assert network.format_duration(None) == "unknown"
    assert network.format_duration(0) == "0s"
    assert network.format_duration(90) == "1m 30s"
    assert network.format_duration(3700) == "1h 1m"
    assert network.format_duration(2 * 86400) == "2d 0h"
    assert network.format_duration(400 * 86400) == "1y 35d"


@pytest.mark.asyncio
async def test_consensus_dialog_renders_the_summary_and_tree():
    app = QApplication.instance() or QApplication([])
    doc = _sample_doc()

    async def fetch():
        return doc

    dialog = katzen.ConsensusDialog(None, fetch)
    await dialog._refresh_async()

    assert dialog._fields["epoch"].text() == str(_EPOCH)
    assert dialog._fields["genesis"].text() == "1000"
    assert dialog._fields["epochs"].text() == str(_EPOCH - 1000)
    assert dialog._fields["period"].text() != "—"
    assert dialog._fields["consensus"].text() != "—"
    # Two mix layers + gateways + service nodes + storage replicas.
    assert dialog._tree.topLevelItemCount() == 5
    assert dialog._tree.topLevelItem(0).child(0).text(0) == "mix1"
    assert dialog._tree.topLevelItem(2).child(0).text(0) == "gw1"
    dialog.deleteLater()
    _ = app


@pytest.mark.asyncio
async def test_consensus_dialog_handles_no_document():
    app = QApplication.instance() or QApplication([])

    async def fetch():
        return None

    dialog = katzen.ConsensusDialog(None, fetch)
    await dialog._refresh_async()
    assert dialog._fields["epoch"].text() == "no PKI document yet"
    dialog.deleteLater()
    _ = app
