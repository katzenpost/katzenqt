"""A simulated Pigeonhole and a scenario language for driving members
against it.

The replica keeps the rules of katzenpost's ``replica/state.go``: boxes are
stored by replica epoch and only the current and previous epochs are kept;
a write to a box that is kept is idempotent if its content is the same and
refused otherwise; a tombstone, the empty payload, is always stored and
shadows what came before.

A scenario is a text file of commands, one per line. A command followed by
a ``----`` line has its output checked against the lines after that, up to
a blank line. ``#`` starts a comment. Positions are printed relative to the
start of each stream.

>>> print(run('''
... new-group alice bob
... send alice hello
... read bob alice
... ----
... '''))
<BLANKLINE>
new-group alice bob
send alice hello
read bob alice
----
alice@0 "hello"
alice@1 not found
<BLANKLINE>
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from hpqc.bacap import MessageBoxIndex, WriteCap

from .member import (
    CTX,
    Effect,
    EpochChanged,
    Event,
    Fetch,
    Found,
    HandOver,
    Introduce,
    Invite,
    Key,
    Member,
    Message,
    Purge,
    Read,
    Remove,
    ScanRequested,
    Send,
    Settings,
    Show,
    Stored,
    Write,
)


@dataclass
class Replica:
    epoch: int = 0
    buckets: dict[int, dict[bytes, Stored]] = field(default_factory=dict)

    def kept(self) -> list[int]:
        return [self.epoch, self.epoch - 1] if self.epoch else [0]

    def write(self, write: Write) -> str:
        if not MessageBoxIndex.verify_box(
            write.box_id, write.payload, write.signature
        ):
            return "bad signature"
        stored = Stored(write.box_id, write.payload, write.signature)
        if write.payload:
            for epoch in self.kept():
                existing = self.buckets.get(epoch, {}).get(write.box_id)
                if existing is not None:
                    return "same" if existing == stored else "refused"
        self.buckets.setdefault(self.epoch, {})[write.box_id] = stored
        return "stored"

    def read(self, box_id: bytes) -> Stored | None:
        for epoch in self.kept():
            stored = self.buckets.get(epoch, {}).get(box_id)
            if stored is not None:
                return stored
        return None

    def advance(self) -> None:
        self.epoch += 1
        kept = self.kept()
        self.buckets = {e: b for e, b in self.buckets.items() if e in kept}


def seeded(name: str) -> Callable[[int], bytes]:
    """Deterministic randomness, so that every run makes the same keys."""
    counter = 0

    def rng(size: int) -> bytes:
        nonlocal counter
        counter += 1
        return hashlib.shake_256(f"{name}/{counter}".encode()).digest(size)

    return rng


class World:
    def __init__(self) -> None:
        self.replica = Replica()
        self.members: dict[str, Member] = {}
        self.joined: set[str] = set()
        self.settings = Settings()

    def run(self, line: str) -> list[str]:
        command, *args = line.split()
        method = getattr(self, "do_" + command.replace("-", "_"), None)
        if method is None:
            raise ValueError(f"unknown command {command!r}")
        return method(*args) or []

    def new_member(self, name: str) -> Member:
        cap = WriteCap.generate(seeded(name))
        member = Member(name, cap, self.settings)
        self.members[name] = member
        return member

    def by_key(self, key: Key) -> Member:
        return next(m for m in self.members.values() if m.key == key)

    def name_of(self, key: Key) -> str:
        """A member's name, or ``removed`` for a placeholder."""
        found = [m.name for m in self.members.values() if m.key == key]
        return found[0] if found else "removed"

    def at(self, key: Key, position: int) -> str:
        """``name@n``: the ``n``th box of ``key``'s stream."""
        owner = self.by_key(key)
        return f"{owner.name}@{position - owner.cap.message_box_index.idx_64}"

    def owner_of(self, index: bytes) -> str:
        """Which stream a ``MessageBoxIndex`` is on, by brute force."""
        position = MessageBoxIndex.from_bytes(index).idx_64
        for member in self.members.values():
            start = member.cap.message_box_index.idx_64
            if 0 <= position - start < 1 << 18:
                return self.at(member.key, position)
        return "?"

    def drive(self, member: Member, event: Event) -> list[str]:
        """Hand ``event`` to ``member`` and carry out its effects until it
        has none left."""
        out: list[str] = []
        pending: list[Effect] = member.handle(event)
        while pending:
            effect = pending.pop(0)
            match effect:
                case Write():
                    result = self.replica.write(effect)
                    if result not in ("stored", "same"):
                        out.append(
                            f"  {self.at(member.key, effect.position)}"
                            f" write {result}"
                        )
                case Fetch(owner, position, box_id, recheck):
                    stored = self.replica.read(box_id)
                    if recheck and (stored is None or not stored.payload):
                        out.append(
                            f"{self.at(owner, position)} recheck:"
                            f" {'not found' if stored is None else 'tombstone'}"
                        )
                    elif stored is None:
                        out.append(f"{self.at(owner, position)} not found")
                        out.extend(self.stuck(member, owner, position))
                    found = Found(owner, position, stored)
                    pending[:0] = member.handle(found)
                case Show(owner, position, message):
                    out.append(
                        f"{self.at(owner, position)} {self.show(message)}"
                    )
                case HandOver(to, joined):
                    new = self.by_key(to)
                    if new.name in self.joined:
                        out.append(f"{new.name} already joined")
                    else:
                        self.joined.add(new.name)
                        new.handle(EpochChanged(self.replica.epoch))
                        new.handle(joined)
        return out

    def stuck(self, member: Member, owner: Key, position: int) -> list[str]:
        heard = member.heard.get(owner)
        if heard is None or heard.index.idx_64 < position:
            return []
        acker = self.by_key(heard.by).name
        return [f"  stuck: {acker} acked {self.at(owner, heard.index.idx_64)}"]

    def show(self, message: Message | None) -> str:
        if message is None:
            return "tombstone"
        parts = []
        if message.text is not None:
            parts.append(f'"{message.text}"')
        if message.introduction is not None:
            parts.append(f"introduces {message.introduction[0]}")
        if message.acks:
            parts.append(
                "acks "
                + " ".join(
                    f"{index}:{self.owner_of(value)}"
                    for index, value in sorted(message.acks.items())
                )
            )
        return " ".join(parts) or "(empty)"

    def do_option(self, option: str, value: str) -> None:
        if self.members:
            raise ValueError("set options before new-group")
        parsed: Any = (
            value == "on"
            if value in ("on", "off")
            else None
            if value == "none"
            else int(value)
        )
        self.settings = replace(
            self.settings, **{option.replace("-", "_"): parsed}
        )

    def do_new_group(self, *names: str) -> None:
        founders = [self.new_member(name) for name in names]
        caps = [founder.read_cap for founder in founders]
        for founder in founders:
            founder.found(caps)
            self.joined.add(founder.name)

    def do_send(self, who: str, *text: str) -> list[str]:
        return self.drive(self.members[who], Send(" ".join(text)))

    def do_invite(self, who: str, new: str) -> list[str]:
        member = self.members.get(new) or self.new_member(new)
        return self.drive(self.members[who], Invite(new, member.read_cap))

    def do_remove(self, who: str, gone: str) -> list[str]:
        return self.drive(self.members[who], Remove(self.members[gone].key))

    def do_inbox(self, who: str) -> list[str]:
        return [
            f"{self.at(owner, position)} \"{text}\""
            for (owner, position), (_, text) in sorted(
                self.members[who].inbox.items(),
                key=lambda item: (self.name_of(item[0][0]), item[0][1]),
            )
        ] or ["empty"]

    def do_peek(self, owner: str, position: str) -> list[str]:
        """What the replica holds at a position: the truth, not any
        member's view."""
        member = self.members[owner]
        start = member.cap.message_box_index
        at = member.read_cap.position_at(
            start.advance_index_to(start.idx_64 + int(position))
        )
        stored = self.replica.read(at.box_id(CTX))
        if stored is None:
            return ["not found"]
        if not stored.payload:
            return ["tombstone"]
        plaintext = at.open(
            CTX, stored.box_id, stored.payload, stored.signature
        )
        return [self.show(Message.decode(plaintext))]

    def do_introduce(self, who: str, new: str) -> list[str]:
        member = self.members.get(new) or self.new_member(new)
        return self.drive(
            self.members[who], Introduce(new, member.read_cap)
        )

    def do_read(self, who: str, owner: str) -> list[str]:
        return self.drive(self.members[who], Read(self.members[owner].key))

    def do_scan(self, who: str, owner: str) -> list[str]:
        return self.drive(
            self.members[who], ScanRequested(self.members[owner].key)
        )

    def do_epoch(self, epochs: str = "1") -> None:
        """Replica epochs pass; nobody backfills."""
        for _ in range(int(epochs)):
            self.replica.advance()

    def do_backfill(self, who: str) -> list[str]:
        return self.drive(self.members[who], EpochChanged(self.replica.epoch))

    def do_tick(self, epochs: str = "1") -> list[str]:
        """Replica epochs pass, and every member backfills at the start of
        each."""
        out: list[str] = []
        for _ in range(int(epochs)):
            self.replica.advance()
            for name in sorted(self.joined):
                out.extend(self.do_backfill(name))
        return out

    def do_purge(self, who: str) -> None:
        self.drive(self.members[who], Purge())

    def do_roster(self, who: str, owner: str) -> list[str]:
        followed = self.members[who].roster(self.members[owner].key)
        if followed is None:
            return ["unknown"]
        line = " ".join(self.name_of(entry) for entry in followed.roster)
        if followed.unsettled:
            line += f" (+{len(followed.unsettled)} unsettled)"
        return [line]

    def do_acks(self, who: str, owner: str, position: str) -> list[str]:
        """How ``who`` resolves the acks in the message at ``position`` on
        ``owner``'s stream."""
        sender = self.members[owner]
        start = sender.cap.message_box_index.idx_64
        reader = self.members[who]
        resolved = reader.resolve(sender.key, start + int(position))
        return [
            " ".join(
                self.at(member, index.idx_64)
                if reader.on_stream(member, index)
                else f"{self.by_key(member).name}:not-on-stream"
                for member, index in resolved.items()
            )
            or "none"
        ]

    def do_acked(self, who: str) -> list[str]:
        member = self.members[who]
        return [
            " ".join(
                f"{self.by_key(acker).name}:{self.at(member.key, position)}"
                for acker, position in sorted(member.acked_by.items())
            )
            or "none"
        ]


def run(script: str) -> str:
    """Run a scenario and return it with every checked command's output
    replaced by what the commands printed now."""
    world = World()
    lines = script.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        command = line.split("#", 1)[0].strip()
        if not command:
            continue
        printed = world.run(command)
        if i < len(lines) and lines[i] == "----":
            out.append("----")
            i += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            out.extend(printed)
    return "\n".join(out) + "\n"
