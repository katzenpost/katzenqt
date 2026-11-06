"""The ``wire_version`` setting: 4 writes every message katzenqt makes in
the array form, byte for byte as pigeonhole_protocols' ``wire.py`` does
against ``groupchat_wire_vectors_v4.json``; 3 writes the map form."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cbor2
import pytest

from katzenqt import acks, headless, models, persistent, prefs
from katzenqt.groupchat import wire
from katzenqt.headless import _cli
from katzenqt.tally import events

V = json.loads(
    (Path(__file__).parent / "data" / "groupchat_wire_vectors_v4.json").read_text()
)
MESSAGES = {e["name"]: e for e in V["messages"]}


@pytest.fixture(autouse=True)
def _restore_wire_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "WIRE_VERSION", models.WIRE_VERSION)


def test_the_setting_is_recorded_and_read_back() -> None:
    assert models.use_wire_version() == 4
    assert models.use_wire_version(3) == 3 == models.WIRE_VERSION
    assert prefs.get(models.WIRE_VERSION_SETTING, None) == 3
    assert models.use_wire_version() == 3
    assert models.use_wire_version(4) == 4 == models.WIRE_VERSION


def test_an_unknown_version_is_refused() -> None:
    with pytest.raises(ValueError, match="wire version 5"):
        models.use_wire_version(5)


def test_a_recorded_version_out_of_range_falls_back_to_4() -> None:
    models.WIRE_VERSION = 3
    with persistent.Session(persistent._engine_sync) as sess:
        sess.add(persistent.AppSetting(id="pref.wire_version", type="int", value="9"))
        sess.commit()
    assert models.use_wire_version() == 4


def _composed(name: str) -> models.GroupChatMessage:
    """The vector's message made afresh, as katzenqt makes one."""
    read = models.GroupChatMessage.from_cbor(bytes.fromhex(MESSAGES[name]["cbor"]))
    return models.GroupChatMessage(
        version=models.WIRE_VERSION,
        membership_hash=read.membership_hash,
        msg_type=read.msg_type,
        text=read.text,
        introduction=read.introduction,
        file_upload=read.file_upload,
        acks=read.acks,
        numbers=read.numbers,
        introduced_at=read.introduced_at,
    )


@pytest.mark.parametrize(
    "name", ["text", "text-unicode", "text-long", "text-with-acks", "control", "introduction", "file-upload"],
)
def test_version_4_composes_as_the_vector(name: str) -> None:
    assert _composed(name).to_cbor().hex() == MESSAGES[name]["cbor"]


@pytest.mark.parametrize("name", ["text", "control", "introduction", "file-upload"])
def test_version_3_composes_the_map_form(name: str) -> None:
    models.WIRE_VERSION = 3
    decoded = cbor2.loads(_composed(name).to_cbor())
    assert isinstance(decoded, dict) and decoded["version"] == 3


def _message(e: dict[str, Any]) -> wire.Message:
    m = e["message"]
    intro = m["introduction"]
    at = m["introduced_at"]
    return wire.Message(
        content=None if m["content"] is None else bytes.fromhex(m["content"]),
        introduction=None if intro is None else wire.Introduction(intro["name"], bytes.fromhex(intro["read_cap"])),
        numbers=tuple((i, None if p is None else bytes.fromhex(p)) for i, p in m["numbers"]),
        introduced_at=None if at is None else wire.IntroRef(at["introducer"], bytes.fromhex(at["index"])),
        acks=dict(m["acks"]),
        membership_hash=None if m["membership_hash"] is None else bytes.fromhex(m["membership_hash"]),
    )


@pytest.mark.parametrize("e", V["written"], ids=[e["name"] for e in V["written"]])
def test_version_4_writes_each_box_as_the_vector(e: dict[str, Any]) -> None:
    assert acks.CODEC.encode(_message(e)).hex() == e["box"][2:]


@pytest.mark.parametrize("e", V["written"], ids=[e["name"] for e in V["written"]])
def test_version_3_writes_control_boxes_as_maps(e: dict[str, Any]) -> None:
    models.WIRE_VERSION = 3
    decoded = cbor2.loads(acks.CODEC.encode(_message(e)))
    # Content keeps the form it was written in; what the codec makes is a map.
    assert isinstance(decoded, list if e["message"]["content"] else dict)


def test_a_tally_is_written_in_the_chosen_form() -> None:
    tally = models.GroupChatTally(survey_id=b"s")
    msg = events._message(models.GroupChatTypeEnum.TALLY_CLOSE, tally).to_cbor()
    assert cbor2.loads(msg) == [4, bytes(32), 7, {"survey_id": b"s", "version": 0}]
    models.WIRE_VERSION = 3
    msg = events._message(models.GroupChatTypeEnum.TALLY_CLOSE, tally).to_cbor()
    assert cbor2.loads(msg)["version"] == 3


def test_headless_flag_is_parsed() -> None:
    assert _cli.parse(["--wire-version", "3", "info"]).wire_version == 3
    assert _cli.parse(["info"]).wire_version is None
    with pytest.raises(SystemExit):
        _cli.parse(["--wire-version", "5", "info"])


def test_headless_flag_sets_and_records_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(persistent, "init_and_migrate", lambda: None)
    headless.cli(["--wire-version", "3", "read", "ghost", "0.1", "--address", "127.0.0.1:64331"])
    assert models.WIRE_VERSION == 3
    assert prefs.get(models.WIRE_VERSION_SETTING, None) == 3
