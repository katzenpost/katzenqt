"""A group chat driven through ``acks`` and ``network``, as the app drives
it, with real BACAP indexes, recording every call made on its state
machine for Cryptlib's replay of the Lean member."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from hpqc.bacap import WriteCap
from sqlmodel import Session

from katzenqt import ack_codec, acks, conversation_handlers, models, network, persistent
from katzenqt.groupchat.sim import seeded, step
from katzenqt.groupchat.trace import Recorder, TracedGroup
from katzenqt.groupchat.wire import counter
from tests.fakes.thinclient import FakeThinClient
from tests.test_acks import _Chat, _epoch, _text

ALICE, BOB, CAROL = (WriteCap.generate(seeded(name)) for name in ("alice", "bob", "carol"))


def _box(cap: WriteCap, n: int) -> bytes:
    index = cap.read_cap().to_bytes()[32:]
    for _ in range(n):
        index = step(index)
    return index


def _read_cap(cap: WriteCap) -> bytes:
    return bytes(cap.read_cap().to_bytes())


async def _group() -> _Chat:
    chat = _Chat()
    chat.write_cap = bytes(ALICE.to_bytes())
    chat.own_cap = _read_cap(ALICE)
    chat.own_key = chat.own_cap[:32]
    await chat.create()
    await chat.add_peer("bob", _read_cap(BOB))
    await chat.add_peer("carol", _read_cap(CAROL))
    founders = [chat.own_cap, _read_cap(BOB), _read_cap(CAROL)]
    group = TracedGroup.found(
        chat.own_cap, founders, codec=acks.CODEC, tracer=Recorder(io.StringIO()),
    )
    async with persistent.asession() as sess:
        row = persistent.GroupState(conversation_id=chat.conversation_id, state=b"")
        await acks._save(sess, acks._Stored(row, group, [], {}, [], []))
        await sess.commit()
    return chat


async def _receive(chat: _Chat, name: str, cap: WriteCap, n: int, gcm: models.GroupChatMessage) -> None:
    peer_id, rcw_id, _ = chat.peers[name]
    async with persistent.asession() as sess:
        peer = await sess.get(persistent.ConversationPeer, peer_id)
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        assert peer is not None and rcw is not None
        rcw.next_index = _box(cap, n + 1)
        await conversation_handlers.dispatch(
            sess, peer, gcm, b"F" + gcm.to_cbor(), box=_box(cap, n),
        )
        await acks.box_read(sess, peer, rcw, _box(cap, n))
        await sess.commit()


def _land(chat: _Chat, pwal_id: Any, n: int) -> None:
    with Session(persistent._engine_sync) as sess:
        pwal = sess.get(persistent.PlaintextWAL, pwal_id)
        assert pwal is not None
        persistent._ensure_sent_log_and_flip_status(sess, pwal)
        persistent._record_sent_box(sess, _box(ALICE, n), pwal)
        sess.delete(pwal)
        sess.commit()


async def _place(chat: _Chat, of: bytes, member: bytes) -> int:
    place = (await chat.group()).rosters.index_of(of, member)
    assert place is not None
    return place


async def run(directory: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Alice reads bob, sends, hears carol has read further on bob's stream,
    scans past a gap there, reads the gap once rewritten, backfills, and
    changes her disappearing messages policy."""
    monkeypatch.setenv(acks.TRACE_DIR, str(directory))
    await _epoch(10)
    chat = await _group()
    bob, carol = _read_cap(BOB)[:32], _read_cap(CAROL)[:32]
    await _receive(chat, "bob", BOB, 0, _text("bob 0"))
    (sent,) = await chat.queue(_text("hi"))
    _land(chat, sent.id, 0)
    acks_of_carol = {
        await _place(chat, carol, bob): counter(_box(BOB, 2)),
        await _place(chat, carol, chat.own_key): counter(_box(ALICE, 0)),
    }
    await _receive(chat, "carol", CAROL, 0, models.GroupChatMessage(
        version=models.GROUP_CHAT_VERSION, membership_hash=bytes(32), text="carol 0", acks=ack_codec.encode(acks_of_carol),
    ))
    client = FakeThinClient()
    client.bacap = True
    scan = {"conversation_id": chat.conversation_id, "peer_id": chat.peers["bob"][0]}
    thin = cast(network.ThinClient, client)
    assert await network.scan_member(thin, **scan) == network.ScanResult(0, True, 1)
    client.box_store[_read_cap(BOB), _box(BOB, 1)] = b"F" + _text("bob 1").to_cbor()
    assert await network.scan_member(thin, **scan) == network.ScanResult(1, False, 0)
    await _epoch(11)
    await acks.tick()
    await persistent.set_retention(chat.conversation_id, persistent.Retention(None, 1))
    await _epoch(13)
    await acks.tick()
    (trace,) = directory.glob("adapter-*.jsonl")
    if keep := os.environ.get("GROUPCHAT_ADAPTER_TRACE_OUT"):
        shutil.copy(trace, Path(keep) / "adapter-scenario.jsonl")
    return [json.loads(line) for line in trace.read_text().splitlines()]


@pytest.mark.asyncio
async def test_every_call_the_app_makes_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await run(tmp_path, monkeypatch)
    assert calls[0]["call"] == "found"
    assert {c["call"] for c in calls} >= {"found", "observe", "compose", "written", "handle"}
    events = [c["args"]["event"]["type"] for c in calls if c["call"] == "handle"]
    assert {"Scan", "Walked", "BoxRead", "EpochChanged"} <= set(events)
    written = next(c for c in calls if c["call"] == "written")
    assert written["args"]["payload"]["bytes"].startswith(b"F".hex()), "the box as written"
    assert calls[-1]["args"]["policy"]["keep_sent"] == 2


@pytest.mark.asyncio
async def test_no_trace_unless_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(acks.TRACE_DIR, raising=False)
    chat = await _Chat().create()
    await chat.queue(_text())
    assert not isinstance(await chat.group(), TracedGroup)


@pytest.mark.nightly
@pytest.mark.skipif(not os.environ.get("CRYPTLIB"), reason="needs a Cryptlib checkout (path in CRYPTLIB)")
@pytest.mark.asyncio
async def test_the_lean_member_agrees_with_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run(tmp_path, monkeypatch)
    lake = shutil.which("lake") or str(Path.home() / ".elan" / "bin" / "lake")
    replay = subprocess.run(
        [lake, "exe", "Cryptlib.Protocols.GroupComm.GroupChatKatzenqt1.replay", str(tmp_path)],
        cwd=os.environ["CRYPTLIB"], capture_output=True, text=True,
    )
    assert replay.returncode == 0, replay.stdout[-4000:]


@pytest.mark.asyncio
async def test_a_group_the_app_starts_is_traced_from_its_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(acks.TRACE_DIR, str(tmp_path))
    chat = await _Chat().create()
    await chat.queue(_text())
    (trace,) = tmp_path.glob("adapter-*.jsonl")
    calls = [json.loads(line)["call"] for line in trace.read_text().splitlines()]
    assert calls == ["found", "compose"]
