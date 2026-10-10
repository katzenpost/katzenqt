"""A simulated Pigeonhole, a driver that plays the thin client daemon with
Python BACAP, and a scenario language for driving group members.

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
from dataclasses import dataclass, field
from typing import Any

from hpqc.bacap import MessageBoxIndex, ReadCap, WriteCap

from .events import (
    BoxRead,
    Deliver,
    DeriveIndexes,
    Effect,
    EpochChanged,
    Event,
    Expire,
    IndexesDerived,
    Introduce,
    Invite,
    Missing,
    Read,
    ReadBox,
    Remove,
    Scan,
    Send,
    SendJoinReply,
    WriteBox,
    WriteTombstone,
)
from .group import Group, Outgoing
from .ledger import Induction
from .sentbox import Retention
from .wire import THIS_MESSAGE, CborCodec, Key, Message, key_of, position

CTX = b"pigeonhole context"


@dataclass(frozen=True)
class Stored:
    box_id: bytes
    payload: bytes
    signature: bytes


@dataclass
class Replica:
    epoch: int = 0
    buckets: dict[int, dict[bytes, Stored]] = field(default_factory=dict)

    def kept(self) -> list[int]:
        return [self.epoch, self.epoch - 1] if self.epoch else [0]

    def write(self, stored: Stored) -> str:
        if not MessageBoxIndex.verify_box(stored.box_id, stored.payload, stored.signature):
            return "bad signature"
        if stored.payload:
            for epoch in self.kept():
                existing = self.buckets.get(epoch, {}).get(stored.box_id)
                if existing is not None:
                    return "same" if existing == stored else "refused"
        self.buckets.setdefault(self.epoch, {})[stored.box_id] = stored
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


@dataclass
class Member:
    name: str
    cap: WriteCap
    group: Group | None = None
    inbox: list[tuple[Key, int, str]] = field(default_factory=list)
    seen: dict[tuple[Key, int], Message] = field(default_factory=dict)
    """Every message this member has read, for ``acks``."""
    reported: int = -1
    """The last of its own boxes a command has printed."""
    passive: bool = False
    """Driven as a client that reads and writes for itself would drive it."""
    unwritten: list[Outgoing] = field(default_factory=list)
    """Messages composed but not yet landed, when driven passively."""
    next_own: bytes = b""
    """The index our next message lands at, when driven passively."""
    cursors: dict[Key, bytes] = field(default_factory=dict)
    """Where we read each stream next, when driven passively."""

    @property
    def read_cap(self) -> bytes:
        return self.cap.read_cap().to_bytes()

    @property
    def key(self) -> Key:
        return key_of(self.read_cap)

    @property
    def start(self) -> int:
        return self.cap.message_box_index.idx_64


class World:
    def __init__(self) -> None:
        self.replica = Replica()
        self.members: dict[str, Member] = {}
        self.codec = CborCodec()
        self.options: dict[str, Any] = {}

    def run(self, line: str) -> list[str]:
        command, *args = line.split()
        method = getattr(self, "do_" + command.replace("-", "_"), None)
        if method is None:
            raise ValueError(f"unknown command {command!r}")
        out: list[str] = method(*args) or []
        return out

    @property
    def joined(self) -> set[str]:
        return {name for name, m in self.members.items() if m.group is not None}

    def member(self, name: str) -> Member:
        return self.members.get(name) or self.new_member(name)

    def new_member(self, name: str) -> Member:
        member = Member(name, WriteCap.generate(seeded(name)))
        self.members[name] = member
        return member

    def group(self, name: str) -> Group:
        group = self.members[name].group
        assert group is not None, f"{name} has not joined"
        return group

    def by_key(self, key: Key) -> Member | None:
        return next((m for m in self.members.values() if m.key == key), None)

    def name_of(self, key: Key | None) -> str:
        if key is not None and key.startswith(b"retired"):
            return "removed"
        member = self.by_key(key) if key is not None else None
        return member.name if member else "?"

    def at(self, key: Key, at: int) -> str:
        member = self.by_key(key)
        assert member is not None
        return f"{member.name}@{at - member.start}"

    def stream_of(self, at: int) -> Member | None:
        """Whose stream an absolute position is on, by brute force."""
        return next(
            (m for m in self.members.values() if 0 <= at - m.start < 1 << 18), None
        )

    def where(self, index: bytes) -> str:
        member = self.stream_of(position(index))
        return self.at(member.key, position(index)) if member else "?"

    def settings(self) -> dict[str, Any]:
        options = {
            k: v for k, v in self.options.items() if k not in ("passive", "delay_writes")
        }
        policy = {
            name: options.pop(name)
            for name in list(options)
            if name in Retention.__dataclass_fields__
        }
        return {"policy": Retention(**policy), **options}

    # The driver.

    def drive(self, member: Member, event: Event) -> list[str]:
        """Hand ``event`` to ``member`` and carry out its effects, depth
        first, until there are none left."""
        group = member.group
        assert group is not None
        out: list[str] = []
        pending: list[Effect] = group.handle(event)
        while pending:
            effect = pending.pop(0)
            follow: Event | None = None
            match effect:
                case WriteBox(index, plaintext):
                    at = member.cap.position_at(MessageBoxIndex.from_bytes(index))
                    written = self.replica.write(Stored(*at.encrypt(CTX, plaintext)))
                    assert written in ("stored", "same"), written
                case WriteTombstone(index):
                    at = member.cap.position_at(MessageBoxIndex.from_bytes(index))
                    box_id, signature = at.tombstone(CTX)
                    self.replica.write(Stored(box_id, b"", signature))
                case ReadBox(stream, read_cap, index, recheck):
                    result, next_index = self.fetch(read_cap, index)
                    out.extend(self.show_read(member, stream, index, result, recheck))
                    follow = BoxRead(stream, index, result, next_index)
                case DeriveIndexes(purpose, stream, start, skip, count):
                    derived = MessageBoxIndex.from_bytes(start)
                    derived = derived.advance_index_to(derived.idx_64 + skip)
                    indexes = []
                    for _ in range(count):
                        indexes.append(derived.to_bytes())
                        derived = derived.next_index()
                    follow = IndexesDerived(purpose, stream, tuple(indexes))
                case Deliver(sender, at_, content):
                    member.inbox.append((sender, at_, content.decode()))
                case Expire(sender, at_):
                    member.inbox = [m for m in member.inbox if m[:2] != (sender, at_)]
                case SendJoinReply(to, reply):
                    new = self.by_key(key_of(to))
                    assert new is not None
                    out.extend(self.join(new, reply))
            if follow is not None:
                pending[:0] = group.handle(follow)
        return out

    def join(self, new: Member, reply: Any) -> list[str]:
        if new.group is not None:
            return [f"{new.name} already joined"]
        new.group = Group.joined(new.read_cap, reply, **self.settings())
        new.group.handle(EpochChanged(self.replica.epoch))
        self.make_passive(new)
        return []

    def make_passive(self, member: Member) -> None:
        if self.options.get("passive"):
            member.passive = True
            member.next_own = member.cap.message_box_index.to_bytes()

    # Driving a member passively.

    def land(self, member: Member) -> None:
        """Write what ``member`` has composed, in order, as a client's own
        write queue would."""
        assert member.group is not None
        for outgoing in member.unwritten:
            plaintext = self.codec.encode(outgoing.message)
            at = member.cap.position_at(MessageBoxIndex.from_bytes(member.next_own))
            written = self.replica.write(Stored(*at.encrypt(CTX, plaintext)))
            assert written in ("stored", "same"), written
            member.group.written(outgoing.token, member.next_own, plaintext)
            member.next_own = at.index.next_index().to_bytes()
        member.unwritten = []

    def queue(self, member: Member, outgoing: list[Outgoing]) -> None:
        member.unwritten += outgoing
        if not self.options.get("delay_writes"):
            self.land(member)

    def read_passively(self, member: Member, owner: Member) -> list[str]:
        group = member.group
        assert group is not None
        reader = group.readers.get(owner.key)
        if reader is None:
            return []
        cursor = member.cursors.get(owner.key, reader.next)
        out = []
        while True:
            result, next_index = self.fetch(reader.read_cap, cursor)
            out.extend(self.show_read(member, owner.key, cursor, result, False))
            if next_index is None:
                break
            for effect in group.observe(
                owner.key, cursor, result if isinstance(result, bytes) else None, next_index
            ):
                if isinstance(effect, Deliver):
                    member.inbox.append((effect.sender, effect.position, effect.content.decode()))
            cursor = next_index
        member.cursors[owner.key] = cursor
        return out

    def fetch(self, read_cap: bytes, index: bytes) -> tuple[bytes | Missing, bytes | None]:
        cap = ReadCap.from_bytes(read_cap)
        at = cap.position_at(MessageBoxIndex.from_bytes(index))
        stored = self.replica.read(at.box_id(CTX))
        if stored is None:
            return Missing.NOT_FOUND, None
        plaintext = at.open(CTX, stored.box_id, stored.payload, stored.signature)
        next_index = at.index.next_index().to_bytes()
        return (plaintext if plaintext else Missing.TOMBSTONE), next_index

    def show_read(
        self, member: Member, stream: Key, index: bytes, result: bytes | Missing, recheck: bool
    ) -> list[str]:
        where = self.at(stream, position(index))
        if recheck and not isinstance(result, bytes):
            return [f"{where} recheck: {result.value}"]
        if isinstance(result, bytes):
            message = self.codec.decode(result)
            assert message is not None
            member.seen[stream, position(index)] = message
            return [f"{where} {self.show(message)}"]
        lines = [f"{where} {result.value}"]
        assert member.group is not None
        heard = member.group.ledger.heard.get(stream)
        if result is Missing.NOT_FOUND and heard and position(heard.index) >= position(index):
            lines.append(f"  stuck: {self.name_of(heard.by)} acked {self.where(heard.index)}")
        return lines

    def show(self, message: Message) -> str:
        parts = []
        if message.content is not None:
            parts.append(f'"{message.content.decode()}"')
        if message.introduction is not None:
            parts.append(f"introduces {message.introduction.name}")
        if message.introduced_at is not None:
            parts.append(f"introduced-at {self.introduced(message.introduced_at.position)}")
        if message.numbers:
            parts.append(
                "numbers "
                + " ".join(
                    f"{index}:"
                    + (
                        message.introduction.name
                        if message.introduction and ref.position == THIS_MESSAGE
                        else self.introduced(ref.position)
                    )
                    for index, ref in message.numbers
                )
            )
        if message.acks:
            parts.append(
                "acks "
                + " ".join(
                    f"{index}:{self.where(value)}"
                    for index, value in sorted(message.acks.items())
                )
            )
        return " ".join(parts) or "(empty)"

    def introduced(self, at: int) -> str:
        """Who the ``Introduction`` at absolute position ``at`` introduced:
        the truth, from its introducer's records."""
        member = self.stream_of(at)
        record = member.group.sent.records.get(at) if member and member.group else None
        message = (
            self.codec.decode(record.plaintext) if record and record.plaintext else None
        )
        if message is not None and message.introduction is not None:
            return message.introduction.name
        return "?"

    # Commands.

    def do_option(self, option: str, value: str) -> None:
        if self.members:
            raise ValueError("set options before new-group")
        parsed: Any = (
            value == "on" if value in ("on", "off")
            else None if value == "none"
            else int(value)
        )
        self.options[option.replace("-", "_")] = parsed

    def do_new_group(self, *names: str) -> None:
        founders = [self.new_member(name) for name in names]
        caps = [founder.read_cap for founder in founders]
        for founder in founders:
            founder.group = Group.found(founder.read_cap, caps, **self.settings())
            self.make_passive(founder)

    def written(self, member: Member) -> list[str]:
        """What ``member`` has written since a command last printed it."""
        lines = []
        assert member.group is not None
        for at, record in sorted(member.group.sent.records.items()):
            if at > member.reported and record.plaintext is not None:
                message = self.codec.decode(record.plaintext)
                assert message is not None
                lines.append(f"{self.at(member.key, at)} {self.show(message)}")
                member.reported = at
        return lines

    def do_send(self, who: str, *text: str) -> list[str]:
        member = self.members[who]
        content = " ".join(text).encode()
        if member.passive:
            assert member.group is not None
            self.queue(member, member.group.compose(content))
            return self.written(member)
        return self.drive(member, Send(content)) + self.written(member)

    def do_invite(self, who: str, new: str) -> list[str]:
        member = self.members[who]
        if member.passive:
            assert member.group is not None
            self.land(member)
            self.queue(member, member.group.begin_induction(new, self.member(new).read_cap))
            self.land(member)
            return self.written(member)
        out = self.drive(member, Invite(new, self.member(new).read_cap))
        return out + self.written(member)

    def do_introduce(self, who: str, new: str) -> list[str]:
        member = self.members[who]
        if member.passive:
            group = member.group
            assert group is not None
            if group.ledger.induction is not Induction.INDUCTING:
                self.do_invite(who, new)
            reply = group.reply(member.next_own)
            self.queue(member, [group.introduce_now()])
            self.land(member)
            return self.join(self.member(new), reply) + self.written(member)
        out = self.drive(member, Introduce(new, self.member(new).read_cap))
        return out + self.written(member)

    def do_land(self, who: str) -> None:
        self.land(self.members[who])

    def do_abandon(self, who: str) -> None:
        """Drop ``who``'s last unwritten content message, as a cancelled
        upload is."""
        member = self.members[who]
        assert member.group is not None
        for outgoing in reversed(member.unwritten):
            if not outgoing.message.control:
                member.unwritten.remove(outgoing)
                member.group.abandoned(outgoing.token)
                return

    def do_remove(self, who: str, gone: str) -> list[str]:
        return self.drive(self.members[who], Remove(self.members[gone].key))

    def do_read(self, who: str, owner: str) -> list[str]:
        member = self.members[who]
        if member.passive:
            return self.read_passively(member, self.members[owner])
        return self.drive(member, Read(self.members[owner].key))

    def do_scan(self, who: str, owner: str) -> list[str]:
        member = self.members[who]
        if member.passive:
            return self.read_passively(member, self.members[owner])
        return self.drive(member, Scan(self.members[owner].key))

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

    def do_roster(self, who: str, owner: str) -> list[str]:
        known = self.group(who).rosters.known(self.members[owner].key)
        if not known:
            return ["unknown"]
        return [" ".join(self.name_of(known.get(i)) for i in range(max(known) + 1))]

    def do_acks(self, who: str, owner: str, at: str) -> list[str]:
        """How ``who`` resolves the acks in ``owner``'s message at ``at``."""
        sender = self.members[owner]
        absolute = sender.start + int(at)
        message = self.members[who].seen.get((sender.key, absolute))
        if message is None:
            return ["not read"]
        rosters = self.group(who).rosters
        return [
            " ".join(
                f"{self.name_of(rosters.resolve(sender.key, index, absolute))}"
                f"={self.where(value)}"
                for index, value in sorted(message.acks.items())
            )
            or "none"
        ]

    def do_acked(self, who: str) -> list[str]:
        member = self.members[who]
        return [
            " ".join(
                f"{self.name_of(acker)}:{self.at(member.key, at)}"
                for acker, at in sorted(self.group(who).ledger.acked_by.items())
            )
            or "none"
        ]

    def do_inbox(self, who: str) -> list[str]:
        return [
            f'{self.at(sender, at)} "{text}"'
            for sender, at, text in self.members[who].inbox
        ] or ["empty"]

    def do_peek(self, owner: str, at: str) -> list[str]:
        """What the replica holds at a position: the truth, not any
        member's view."""
        member = self.members[owner]
        index = member.cap.message_box_index
        index = index.advance_index_to(index.idx_64 + int(at))
        result, _ = self.fetch(member.read_cap, index.to_bytes())
        if isinstance(result, bytes):
            message = self.codec.decode(result)
            assert message is not None
            return [self.show(message)]
        return [result.value]


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
