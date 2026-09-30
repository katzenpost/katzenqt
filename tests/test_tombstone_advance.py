"""A tombstone consumes its position; a missing box does not.

Kept in its own file so these cases do not share hunks with the large
annotated test modules.
"""

import pytest
from katzenpost_thinclient import BoxIDNotFoundError, TombstoneError

from katzenqt import network, persistent

from tests.test_network_fake import _set_up_read_flow


@pytest.mark.asyncio
async def test_a_tombstone_advances_the_read_index(fake_thinclient) -> None:
    """A tombstone is a position that was written and then deleted, so the
    reader consumes it and moves to the next index. Retrying it instead stalls
    that reader for good, and because a stream's owner may tombstone its own
    boxes, any member could otherwise halt every reader of its own stream."""
    setup = await _set_up_read_flow(fake_thinclient)
    fake_thinclient.inject_error(
        "start_resending_encrypted_message",
        TombstoneError("tombstone"),
    )
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        expected = mw.next_message_index
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw.next_index == mw.current_message_index
    draining: "set" = {setup["bacap_stream"]}
    await network.drain_mixwal_read_single(
        connection=fake_thinclient,
        rcw_read_cap=setup["read_cap"],
        mw=mw,
        draining_right_now=draining,
    )
    async with persistent.asession() as sess:
        assert await sess.get(persistent.MixWAL, setup["mw_id"]) is None
        rcw = await sess.get(persistent.ReadCapWAL, setup["bacap_stream"])
        assert rcw.next_index == expected
    assert setup["bacap_stream"] not in draining


@pytest.mark.asyncio
async def test_a_missing_box_keeps_the_read_index(fake_thinclient) -> None:
    """A box that is merely not found may still arrive, so the row stays for a
    later retry at the same index."""
    setup = await _set_up_read_flow(fake_thinclient)
    fake_thinclient.inject_error(
        "start_resending_encrypted_message",
        BoxIDNotFoundError("box ID not found"),
    )
    async with persistent.asession() as sess:
        mw = await sess.get(persistent.MixWAL, setup["mw_id"])
        before = (
            await sess.get(
                persistent.ReadCapWAL,
                setup["bacap_stream"],
            )
        ).next_index
    draining: "set" = {setup["bacap_stream"]}
    await network.drain_mixwal_read_single(
        connection=fake_thinclient,
        rcw_read_cap=setup["read_cap"],
        mw=mw,
        draining_right_now=draining,
    )
    async with persistent.asession() as sess:
        assert await sess.get(persistent.MixWAL, setup["mw_id"]) is not None
        assert (
            await sess.get(
                persistent.ReadCapWAL,
                setup["bacap_stream"],
            )
        ).next_index == before
    assert setup["bacap_stream"] not in draining
