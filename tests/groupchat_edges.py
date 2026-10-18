"""Members driven past what the scenarios reach, recorded for CryptWalker's
replay as ``edge-*.jsonl``: each function here drives one member through a
case the scenario language cannot set up, and returns it for its test to
check."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

from hpqc.bacap import WriteCap

from katzenqt.groupchat.events import EpochChanged, Invite, JoinReply, Scan, Send
from katzenqt.groupchat.sentbox import Retention
from katzenqt.groupchat.sim import seeded, step
from katzenqt.groupchat.trace import Recorder, TracedGroup
from katzenqt.groupchat.wire import ROSTER_MAX, Introduction, Message, counter, key_of


def read_cap(name: str) -> bytes:
    return bytes(WriteCap.generate(seeded(name)).read_cap().to_bytes())


def boxes(cap: bytes, n: int) -> list[bytes]:
    """The first ``n`` boxes of the stream ``cap`` reads."""
    index = cap[32:]
    out = []
    for _ in range(n):
        out.append(index)
        index = step(index)
    return out


def roster_full(trace: IO[str]) -> TracedGroup:
    """Zed joins a long-lived group whose introducer, Ann, has numbered 250
    members, all since removed, and reads Ann's Introductions of six more.
    Zed numbers the four its roster has room for and leaves the rest
    unnumbered; Ann gave the last two no number either."""
    ann, zed = read_cap("ann"), read_cap("zed")
    newcomers = [read_cap(f"new{n}") for n in range(6)]
    places = ROSTER_MAX - 5
    reply = JoinReply(
        members=((ann, ann[32:]),) + (None,) * (places - 1),
        rosters=(tuple((i, i) for i in range(places)),) + (None,) * (places - 1),
        introductions=(),
        introducer=0,
        newcomer=places,
    )
    group = TracedGroup.joined(zed, reply, tracer=Recorder(trace))
    introduced = [(zed, ((places, None),))] + [
        (cap, ((places + 1 + n, None),) if places + 1 + n < ROSTER_MAX else ())
        for n, cap in enumerate(newcomers)
    ]
    at = boxes(ann, len(introduced) + 1)
    for n, (cap, numbers) in enumerate(introduced):
        message = Message(introduction=Introduction(f"m{n}", cap), numbers=numbers)
        group.observe(key_of(ann), at[n], group.codec.encode(message), at[n + 1])
    own = boxes(zed, 3)
    for n, outgoing in enumerate(group.compose(b"hi") + group.compose(b"again")):
        group.written(outgoing.token, own[n], group.codec.encode(outgoing.message))
    return group


def retired_prefix(trace: IO[str]) -> TracedGroup:
    """Alice removes Carol. Bob then numbers an index by the prefix that
    stands in Carol's place, and acknowledges a box at it: a prefix names
    only a member whose key is known, so Alice resolves neither."""
    alice, bob, carol = (read_cap(name) for name in ("alice", "bob", "carol"))
    group = TracedGroup.found(alice, [alice, bob, carol], tracer=Recorder(trace))
    group.remove(key_of(carol))
    (token,) = (
        entry.key
        for entry in group.rosters.entries[group.me].values()
        if entry.key is not None and entry.key.startswith(b"retired")
    )
    at = boxes(bob, 2)
    message = Message(content=b"hi", numbers=((3, token),), acks={3: 5})
    group.observe(key_of(bob), at[0], group.codec.encode(message), at[1])
    return group


def bounded_state(trace: IO[str]) -> TracedGroup:
    """What a member remembers stays bounded. Alice keeps no record of what
    she reads while no policy expires it, and forgets the record once the
    policy stops. A scan looks only for acknowledged boxes ahead of where it
    is, and remembers looking only while someone still acknowledges them."""
    alice, bob, carol = (read_cap(name) for name in ("alice", "bob", "carol"))
    group = TracedGroup.found(alice, [alice, bob, carol], tracer=Recorder(trace))
    on_bob, on_carol = boxes(bob, 14), boxes(carol, 4)
    bob_at_carol = group.rosters.index_of(key_of(carol), key_of(bob))
    assert bob_at_carol is not None

    def read(stream: bytes, at: list[bytes], n: int, message: Message) -> None:
        group.observe(key_of(stream), at[n], group.codec.encode(message), at[n + 1])

    read(bob, on_bob, 0, Message(content=b"one"))
    read(bob, on_bob, 1, Message(content=b"two"))
    group.policy = Retention(keep_received=2)
    read(bob, on_bob, 2, Message(content=b"three"))
    group.policy = Retention()
    group.handle(EpochChanged(1))
    for n, acked in enumerate((0, 10, 12)):
        read(carol, on_carol, n, Message(content=b"seen", acks={bob_at_carol: counter(on_bob[acked])}))
        group.handle(Scan(key_of(bob)))
    return group


def client_writes(trace: IO[str]) -> TracedGroup:
    """Alice's client writes for her, from her stream's first box. Events
    that would have her write, or derive boxes to write, then change only
    what she owes: her pool would name the boxes her client hands out."""
    alice, bob, dave = (read_cap(name) for name in ("alice", "bob", "dave"))
    group = TracedGroup.found(alice, [alice, bob], tracer=Recorder(trace))
    own = boxes(alice, 2)
    (outgoing,) = group.compose(b"hi")
    group.written(outgoing.token, own[0], group.codec.encode(outgoing.message))
    group.handle(Send(b"again"))
    group.handle(Invite("dave", dave))
    return group


def shared_prefix(trace: IO[str]) -> TracedGroup:
    """Bob introduces two members whose keys begin alike, as far as a
    ``Numbers`` field names them. Alice numbers one in each control message,
    so that each field names its members apart."""
    alice, bob = read_cap("alice"), read_cap("bob")
    group = TracedGroup.found(alice, [alice, bob], tracer=Recorder(trace))
    twins = [b"twin" * 2 + bytes([n]) * 24 + read_cap(f"twin{n}")[32:] for n in range(2)]
    at = boxes(bob, 3)
    for n, cap in enumerate(twins):
        message = Message(introduction=Introduction(f"twin{n}", cap), numbers=((2 + n, None),))
        group.observe(key_of(bob), at[n], group.codec.encode(message), at[n + 1])
    own = boxes(alice, 2)
    for n in range(2):
        (control,) = group.compose()
        group.written(control.token, own[n], group.codec.encode(control.message))
    return group


EDGES: dict[str, Callable[[IO[str]], TracedGroup]] = {
    "client_writes": client_writes,
    "bounded_state": bounded_state,
    "retired_prefix": retired_prefix,
    "roster_full": roster_full,
    "shared_prefix": shared_prefix,
}


def record(out: Path) -> None:
    for name, edge in EDGES.items():
        with (out / f"edge-{name}.jsonl").open("w") as trace:
            edge(trace)
