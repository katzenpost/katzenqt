import struct
from uuid import UUID

import pytest
from sqlmodel import select

from katzenqt import network, persistent


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True])
async def test_completion_removes_only_the_matching_release(
    extended: bool,
) -> None:
    parent = UUID(int=10)
    other_parent = UUID(int=11)
    read_cap = b"r" * 136
    body = struct.pack(">I", 3) + read_cap if extended else read_cap
    async with persistent.asession() as sess:
        for index, stream, kind, chunk in (
            (1, parent, b"I", body),
            (2, parent, b"I", b"s" * 136),
            (3, other_parent, b"I", body),
            (4, parent, b"C", body),
            (5, parent, b"I", b"x" + read_cap),
        ):
            sess.add(persistent.ReceivedPiece(
                read_cap=stream, bacap_index=index.to_bytes(8, "little"),
                chunk_type=kind, chunk=chunk,
            ))
        await sess.commit()
        await network._discard_substream_release(sess, parent, read_cap)
        await sess.commit()
    async with persistent.asession() as sess:
        rows = (await sess.exec(select(persistent.ReceivedPiece))).all()
        assert {int.from_bytes(row.bacap_index, "little") for row in rows} == {
            2, 3, 4, 5,
        }
