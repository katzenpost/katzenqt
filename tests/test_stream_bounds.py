from __future__ import annotations

import logging
import uuid

import pytest

from katzenqt import models, network, persistent

from tests.fakes.thinclient import FakeThinClient
from tests.test_network_fake import _make_F_payload, _set_up_read_flow

BODY = 1024


def _idx(counter: int) -> bytes:
    return counter.to_bytes(8, "little") + bytes(96)


async def _lay_a_real_chain(stream: uuid.UUID, text_chars: int) -> int:
    blob = models.GroupChatMessage(
        version=0, membership_hash=b"X" * 32, text="x" * text_chars,
    ).to_cbor()
    bodies = [blob[off:off + BODY] for off in range(0, len(blob), BODY)]
    async with persistent.asession() as sess:
        for n, body in enumerate(bodies):
            sess.add(persistent.ReceivedPiece(
                read_cap=stream, bacap_index=_idx(n)[:8],
                chunk_type=b"F" if n == len(bodies) - 1 else b"C",
                chunk=body,
            ))
        await sess.commit()
    return len(bodies) - 1


async def _peer_chain(hops: int) -> persistent.ConversationPeer:
    async with persistent.asession() as sess:
        rc = uuid.uuid4()
        sess.add(persistent.ReadCapWAL(id=rc, read_cap=bytes([3]) * 136))
        cur = persistent.ConversationPeer(name="alice", read_cap_id=rc)
        sess.add(cur)
        await sess.commit()
        await sess.refresh(cur)
        for hop in range(hops):
            child_rc = uuid.uuid4()
            sess.add(persistent.ReadCapWAL(
                id=child_rc, read_cap=bytes([4 + hop]) * 136,
            ))
            child = persistent.ConversationPeer(
                name=f"{network._SUBSTREAM_NAME_PREFIX}{cur.id}:{hop:04x}",
                read_cap_id=child_rc,
            )
            sess.add(child)
            await sess.commit()
            await sess.refresh(child)
            cur = child
        return cur


@pytest.mark.asyncio
async def test_a_chain_inside_the_ceiling_assembles() -> None:
    stream = uuid.uuid4()
    terminal = await _lay_a_real_chain(stream, text_chars=2048)
    async with persistent.asession() as sess:
        assembled = await network._try_assemble(
            sess, stream, _idx(terminal)[:8],
        )
    assert assembled is not None and assembled[0] == "F"


@pytest.mark.asyncio
async def test_a_chain_past_the_ceiling_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = uuid.uuid4()
    terminal = await _lay_a_real_chain(stream, text_chars=40 * 1024)
    monkeypatch.setattr(network, "_ASSEMBLY_HARD_CAP", 4096)
    async with persistent.asession() as sess:
        with caplog.at_level(logging.WARNING, logger=network.logger.name):
            assembled = await network._try_assemble(
                sess, stream, _idx(terminal)[:8],
            )
    assert assembled is None
    assert any("ceiling" in r.message for r in caplog.records)


def test_the_ceiling_leaves_room_for_an_oversized_file() -> None:
    headroom = network._ASSEMBLY_HARD_CAP - network._ATTACHMENT_HARD_CAP
    assert headroom == 1024 * 1024
    assert network._ASSEMBLY_HARD_CAP == 210763776


def test_the_bounds_match_katzen_core() -> None:
    assert network._MAX_INDIRECTION_DEPTH == 8
    assert network._MAX_OPEN_PIECES_PER_STREAM == 300_000
    assert network._ASSEMBLY_HARD_CAP // 1530 < 300_000


@pytest.mark.asyncio
async def test_depth_counts_the_substream_hops() -> None:
    assert await _depth(await _peer_chain(0)) == 0
    assert await _depth(await _peer_chain(1)) == 1
    deep = await _peer_chain(network._MAX_INDIRECTION_DEPTH + 1)
    assert await _depth(deep) > network._MAX_INDIRECTION_DEPTH


async def _depth(peer: persistent.ConversationPeer) -> int:
    async with persistent.asession() as sess:
        return await network._indirection_depth(sess, peer)


@pytest.mark.asyncio
async def test_a_malformed_substream_name_stops_the_walk() -> None:
    async with persistent.asession() as sess:
        rc = uuid.uuid4()
        sess.add(persistent.ReadCapWAL(id=rc, read_cap=bytes([9]) * 136))
        orphan = persistent.ConversationPeer(
            name=f"{network._SUBSTREAM_NAME_PREFIX}not-a-number:ab",
            read_cap_id=rc,
        )
        sess.add(orphan)
        await sess.commit()
        await sess.refresh(orphan)
        assert await network._indirection_depth(sess, orphan) == 1


@pytest.mark.asyncio
async def test_pieces_are_counted_per_stream() -> None:
    mine = uuid.uuid4()
    async with persistent.asession() as sess:
        for n in range(3):
            sess.add(persistent.ReceivedPiece(
                read_cap=mine, bacap_index=_idx(n)[:8],
                chunk_type=b"C", chunk=b"x",
            ))
        await sess.commit()
    async with persistent.asession() as sess:
        assert await network._open_piece_count(sess, mine) == 3
        assert await network._open_piece_count(sess, uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_a_full_stream_drops_the_chunk(
    fake_thinclient: FakeThinClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await _set_up_read_flow(
        fake_thinclient, plaintext=_make_F_payload("hi"),
    )
    monkeypatch.setattr(network, "_MAX_OPEN_PIECES_PER_STREAM", 0)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    with caplog.at_level(logging.WARNING, logger=network.logger.name):
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
    assert any("un-coalesced" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_peer_too_deep_is_retired(
    fake_thinclient: FakeThinClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await _set_up_read_flow(
        fake_thinclient, plaintext=_make_F_payload("hi"),
    )
    monkeypatch.setattr(network, "_MAX_INDIRECTION_DEPTH", -1)
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    with caplog.at_level(logging.WARNING, logger=network.logger.name):
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
    assert any("hops deep" in r.message for r in caplog.records)
