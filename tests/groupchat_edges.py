"""Members driven past what the scenarios reach, recorded for CryptWalker's
replay as ``edge-*.jsonl``: each function here drives one member through a
case the scenario language cannot set up, and returns it for its test to
check."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

from hpqc.bacap import WriteCap

from katzenqt.groupchat.events import JoinReply
from katzenqt.groupchat.sim import seeded, step
from katzenqt.groupchat.trace import Recorder, TracedGroup
from katzenqt.groupchat.wire import ROSTER_MAX, Introduction, Message, key_of


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


EDGES: dict[str, Callable[[IO[str]], TracedGroup]] = {"roster_full": roster_full}


def record(out: Path) -> None:
    for name, edge in EDGES.items():
        with (out / f"edge-{name}.jsonl").open("w") as trace:
            edge(trace)
