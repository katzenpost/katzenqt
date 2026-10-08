from __future__ import annotations

import logging
import uuid

import pytest
from sqlmodel import select

from katzenqt import network, persistent

from tests.fakes.thinclient import FakeThinClient
from tests.test_network_fake import _make_F_payload, _set_up_read_flow


@pytest.mark.asyncio
async def test_a_reply_that_skips_a_box_does_not_move_the_stream(
    fake_thinclient: FakeThinClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await _set_up_read_flow(
        fake_thinclient, plaintext=_make_F_payload("hi"),
    )
    here = setup["first_message_index"]

    async def counter(message_box_index: bytes) -> int:
        return 5 if message_box_index == here else 9

    monkeypatch.setattr(
        fake_thinclient, "get_message_box_index_counter", counter,
    )
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        assert mw is not None
    with caplog.at_level(logging.WARNING, logger=network.logger.name):
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=setup["read_cap"],
            mw=mw, draining_right_now={setup["bacap_stream"]},
        )
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw is not None and rcw.next_index == here
        logged = (await sess.exec(
            select(persistent.ConversationLog)
        )).all()
        assert logged == []
    assert any("skips a box" in r.message for r in caplog.records)
