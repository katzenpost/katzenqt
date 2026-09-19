from uuid import UUID

import pytest

from katzenqt import network, persistent, qt_models


@pytest.mark.asyncio
async def test_restart_keeps_zero_piece_pauses_and_failures() -> None:
    own = UUID(int=1)
    paused = UUID(int=2)
    failed = UUID(int=3)
    done = UUID(int=4)
    async with persistent.asession() as sess:
        sess.add(persistent.ReadCapWAL(id=own))
        parent = persistent.ConversationPeer(name="bob", read_cap_id=own)
        conv = persistent.Conversation(name="demo", own_peer=parent)
        conv.peers.append(parent)
        sess.add(conv)
        await sess.commit()
        await sess.refresh(parent)
        await sess.refresh(conv)
        parent_id = parent.id
        for stream, active, read_paused, failure in (
            (paused, True, True, None),
            (failed, False, False, "bad frame"),
            (done, False, False, None),
        ):
            sess.add(persistent.ReadCapWAL(
                id=stream, read_cap=b"r" * 136, next_index=b"i" * 104,
                read_paused=read_paused, substream_failure=failure,
            ))
            sess.add(persistent.ConversationPeer(
                name=f":substream:{parent_id}:{stream.int}",
                read_cap_id=stream, active=active, conversation=conv,
            ))
        await sess.commit()
    model = qt_models.DownloadsModel()
    await model.seed_from_db()
    assert model.rowCount() == 2
    states = {
        model.data(model.index(i, 0), qt_models.ROLE_TRANSFER_RCW_ID): (
            model.data(model.index(i, 0), qt_models.ROLE_TRANSFER_ACTIVE),
            model.data(model.index(i, 0), qt_models.ROLE_TRANSFER_FAILED),
        ) for i in range(model.rowCount())
    }
    assert states == {str(paused): (False, False), str(failed): (False, True)}
    await network.resume_peer_reads(bacap_stream=paused)
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, paused)
        assert rcw is not None
        assert not rcw.read_paused
        assert rcw.next_index == b"i" * 104
    await network.dismiss_failed_transfer(bacap_stream=failed)
    model = qt_models.DownloadsModel()
    await model.seed_from_db()
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), qt_models.ROLE_TRANSFER_RCW_ID) == str(paused)
