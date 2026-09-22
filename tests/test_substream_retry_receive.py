from typing import cast
from uuid import UUID

import pytest
from sqlmodel import select
from katzenpost_thinclient import BoxIDNotFoundError

from katzenqt import network, persistent
from tests.fakes.thinclient import FakeThinClient
from tests.test_network_fake import _set_up_read_flow


@pytest.mark.asyncio
async def test_missing_substream_box_then_data_uses_the_same_cursor(
    fake_thinclient: FakeThinClient,
) -> None:
    setup = await _set_up_read_flow(
        fake_thinclient, peer_name=":substream:99:test", plaintext=b"Cdata",
    )
    stream = cast(UUID, setup["bacap_stream"])
    read_cap = cast(bytes, setup["read_cap"])
    mw_id = cast(UUID, setup["mw_id"])
    fake_thinclient.inject_error(
        "start_resending_encrypted_message", BoxIDNotFoundError("not yet"),
    )
    for number in range(2):
        async with persistent.asession() as sess:
            mw = await sess.get(persistent.MixWAL, mw_id)
            assert mw is not None
        await network.drain_mixwal_read_single(
            connection=fake_thinclient, rcw_read_cap=read_cap,
            mw=mw, draining_right_now={stream},
        )
        async with persistent.asession() as sess:
            rcw = await sess.get(persistent.ReadCapWAL, stream)
            assert rcw is not None
            if number == 0:
                assert rcw.next_index == setup["first_message_index"]
                assert rcw.substream_missing_since is not None
            else:
                assert rcw.next_index == setup["rcr"].next_message_box_index
                assert rcw.substream_missing_since is None
            peer = (await sess.exec(select(persistent.ConversationPeer))).one()
            assert peer.active
    events = []
    while not network.substream_progress_queue.empty():
        events.append(network.substream_progress_queue.get_nowait())
    assert [event[0] for event in events] == ["piece"]
