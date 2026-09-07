from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from katzenqt import persistent
from katzenqt.headless import _actions, _cli
from tests.stubs import returning

OWN_READ_CAP = bytes([0x11]) * 136
WRITE_CAP = bytes([0x22]) * 168


async def run_action(argv: "list[str]") -> int:
    return int(await _cli.parse(argv).run())


class FakeClock:

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float, result: object = None) -> object:
        self.now += delay
        return result

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_actions, "asyncio", SimpleNamespace(
            get_event_loop=returning(SimpleNamespace(time=self.time)),
            sleep=self.sleep,
        ))


class StubConnection:

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def peer_read_cap(index: int) -> bytes:
    return bytes([0x30 + index]) * 136


@dataclass
class ConvHandle:
    conversation_id: int
    own_peer_id: int
    write_cap_id: uuid.UUID
    own_read_cap_id: uuid.UUID
    peer_ids: "dict[str, int]" = field(default_factory=dict)


async def make_conversation(
    name: str,
    *,
    own_name: str = "me",
    peers: "tuple[str, ...]" = (),
    provision_write_cap: bool = True,
    provision_read_cap: bool = True,
) -> ConvHandle:
    wcap = persistent.WriteCapWAL(
        id=uuid.uuid4(),
        write_cap=WRITE_CAP if provision_write_cap else None,
    )
    own_rcap = persistent.ReadCapWAL(
        id=uuid.uuid4(),
        write_cap_id=wcap.id,
        read_cap=OWN_READ_CAP if provision_read_cap else None,
    )
    convo = persistent.Conversation(name=name, write_cap=wcap.id, first_unread=0)
    own_peer = persistent.ConversationPeer(
        name=own_name, read_cap_id=own_rcap.id, conversation=convo,
    )
    convo.own_peer = own_peer

    peer_ids: "dict[str, int]" = {}
    async with persistent.asession() as sess:
        sess.add(wcap)
        sess.add(own_rcap)
        sess.add(convo)
        sess.add(own_peer)
        extra = []
        for index, peer_name in enumerate(peers):
            rcap = persistent.ReadCapWAL(
                id=uuid.uuid4(), read_cap=peer_read_cap(index),
            )
            peer = persistent.ConversationPeer(
                name=peer_name, read_cap_id=rcap.id, conversation=convo,
            )
            sess.add(rcap)
            sess.add(peer)
            extra.append((peer_name, peer))
        await sess.flush()
        for peer_name, peer in extra:
            peer_ids[peer_name] = peer.id
        handle = ConvHandle(
            conversation_id=convo.id,
            own_peer_id=own_peer.id,
            write_cap_id=wcap.id,
            own_read_cap_id=own_rcap.id,
            peer_ids=peer_ids,
        )
        await sess.commit()
    return handle


async def add_log_row(
    *,
    conversation_id: int,
    conversation_peer_id: int,
    conversation_order: int,
    payload: bytes,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    row = persistent.ConversationLog(
        id=row_id,
        conversation_id=conversation_id,
        conversation_peer_id=conversation_peer_id,
        conversation_order=conversation_order,
        payload=payload,
    )
    async with persistent.asession() as sess:
        sess.add(row)
        await sess.commit()
    return row_id
