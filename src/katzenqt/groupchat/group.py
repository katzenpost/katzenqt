"""One member of one group chat, as a sans-io state machine. :meth:`Group.handle`
takes an :mod:`~katzenqt.groupchat.events` event and returns the effects
the driver must carry out. The parts each own one concern: a
:class:`~.reader.Reader` per stream, the :class:`~.sentbox.SentBox`, the
:class:`~.ledger.Ledger` of acknowledgements and the
:class:`~.numbering.Rosters`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    JoinReply,
    Missing,
    Read,
    Remove,
    Scan,
    Send,
    SendJoinReply,
    WriteBox,
)
from .ledger import Induction, Ledger
from .numbering import At, Entry, Rosters
from .reader import Mode, Reader
from .sentbox import Retention, SentBox
from .wire import (
    KEY_SIZE,
    CborCodec,
    Codec,
    Introduction,
    IntroRef,
    Key,
    Message,
    key_of,
    position,
)

POOL = 4
"""How many of our own indexes to have derived ahead."""


def placeholder(place: int) -> Key:
    """What stands in a roster for a member known only by its place."""
    return b"retired" + bytes([place])


@dataclass
class Group:
    read_cap: bytes
    """Our own read cap: our key and our stream's first index."""
    policy: Retention = field(default_factory=Retention)
    recheck: int = 4
    """How many positions behind a reader keeps indexes for rechecking."""
    auto_scan: bool = True
    codec: Codec = field(default_factory=CborCodec, compare=False, repr=False)
    rosters: Rosters = field(default_factory=Rosters)
    readers: dict[Key, Reader] = field(default_factory=dict)
    sent: SentBox = field(default_factory=SentBox)
    ledger: Ledger = field(default_factory=Ledger)
    epoch: int = 0
    pool: list[bytes] = field(default_factory=list)
    tip: bytes = b""
    """The furthest of our own indexes derived so far."""
    deriving: bool = False
    outbox: list[Message] = field(default_factory=list)
    """Messages waiting for an index; acks and numbers are added when they
    get one."""
    unnumbered: list[At] = field(default_factory=list)
    """Introductions read whose member we have not numbered yet."""
    introduced_at: IntroRef | None = None
    """Owed in our first message, if we joined."""
    awaiting_start: dict[Key, int] = field(default_factory=dict)
    """``IntroducedAt`` read before the ``Introduction`` it names."""
    received: dict[tuple[Key, int], int] = field(default_factory=dict)
    """Content delivered, by sender and position, and the epoch it came."""

    def __post_init__(self) -> None:
        if not self.tip:
            self.tip = self.read_cap[KEY_SIZE:]
            self.pool = [self.tip]

    @property
    def me(self) -> Key:
        return key_of(self.read_cap)

    @classmethod
    def found(cls, read_cap: bytes, founders: list[bytes], **kwargs: Any) -> Group:
        """Start a group with ``founders``' read caps, ours among them."""
        group = cls(read_cap, **kwargs)
        group.rosters.found([key_of(cap) for cap in founders])
        for cap in founders:
            if key_of(cap) != group.me:
                group._follow(cap, cap[KEY_SIZE:])
        return group

    @classmethod
    def joined(cls, read_cap: bytes, reply: JoinReply, **kwargs: Any) -> Group:
        """Join a group from what our introducer handed us."""
        group = cls(read_cap, **kwargs)
        order = [
            key_of(member[0]) if member else placeholder(place)
            for place, member in enumerate(reply.members)
        ]
        for place, roster in enumerate(reply.rosters):
            if roster is not None:
                group.rosters.entries[order[place]] = {
                    index: Entry(order[slot] if slot is not None else None, at=at)
                    for index, slot, at in roster
                }
        for introducer, at, member in reply.introductions:
            group.rosters.introductions[order[introducer], at] = order[member]
        for slot in reply.members:
            if slot and key_of(slot[0]) != group.me:
                group._follow(*slot)
        group.introduced_at = IntroRef(reply.introducer, reply.introduction)
        return group

    def handle(self, event: Event) -> list[Effect]:
        match event:
            case Send(content):
                self.outbox.append(Message(content=content))
                return self._progress()
            case Read(stream) if stream in self.readers:
                return self.readers[stream].read()
            case Scan(stream) if stream in self.readers:
                return self._scan(stream)
            case BoxRead():
                return self._box_read(event)
            case IndexesDerived(purpose, stream, indexes):
                return self._derived(purpose, stream, indexes)
            case Invite(name, read_cap):
                return self._invite(name, read_cap, commit=False)
            case Introduce(name, read_cap):
                if self.ledger.induction is Induction.IDLE:
                    return self._invite(name, read_cap, commit=True)
                self.ledger.commit = True
                return self._progress()
            case Remove(member):
                self._remove(member)
                return []
            case EpochChanged(epoch):
                return self._epoch(epoch)
        return []

    # Reading.

    def _follow(self, read_cap: bytes, start: bytes) -> None:
        stream = key_of(read_cap)
        self.readers[stream] = Reader(stream, read_cap, start)

    def _scan(self, stream: Key) -> list[Effect]:
        heard = self.ledger.heard.get(stream)
        return self.readers[stream].scan(heard.index if heard else None, self.recheck)

    def _box_read(self, event: BoxRead) -> list[Effect]:
        reader = self.readers.get(event.stream)
        if reader is None:
            return []
        plaintext, effects = reader.found(
            event.index, event.result, event.next_index, self.recheck
        )
        if plaintext is not None:
            effects = self._ingest(event.stream, position(event.index), plaintext) + effects
        if event.result is Missing.NOT_FOUND and reader.mode is Mode.IDLE:
            heard = self.ledger.heard.get(event.stream)
            if (
                self.auto_scan
                and heard is not None
                and position(heard.index) > max(reader.position, reader.scanned_to)
            ):
                effects += self._scan(event.stream)
        return effects + self._progress()

    def _ingest(self, sender: Key, at: int, plaintext: bytes) -> list[Effect]:
        message = self.codec.decode(plaintext)
        if message is None:
            return []
        for index, ref in message.numbers:
            introducer = self.rosters.resolve(sender, ref.introducer)
            self.rosters.number(
                sender, index, (introducer, ref.position) if introducer else None, at
            )
        if message.introduction is not None:
            self._introduced(sender, at, message.introduction)
        if message.introduced_at is not None:
            self._started(sender, message.introduced_at.position)
        for index, value in message.acks.items():
            acked = self.rosters.resolve(sender, index, at)
            if acked == self.me:
                if self.sent.check(position(value), value):
                    self.ledger.acked(sender, position(value))
            elif acked is not None:
                self.ledger.hear(acked, value, sender)
        if message.content is None:
            return []
        self.received[sender, at] = self.epoch
        return [Deliver(sender, at, message.content)]

    def _introduced(self, introducer: Key, at: int, introduction: Introduction) -> None:
        new = key_of(introduction.read_cap)
        self.rosters.introductions[introducer, at] = new
        if self.awaiting_start.get(new) == at:
            del self.awaiting_start[new]
            self.rosters.started[new] = (introducer, at)
        if new == self.me or self.rosters.index_of(self.me, new) is not None:
            return
        if new not in self.readers:
            self._follow(introduction.read_cap, introduction.read_cap[KEY_SIZE:])
        if (introducer, at) not in self.unnumbered:
            self.unnumbered.append((introducer, at))

    def _started(self, member: Key, at: int) -> None:
        for (introducer, intro_at), new in self.rosters.introductions.items():
            if new == member and intro_at == at:
                self.rosters.started[member] = (introducer, at)
                return
        self.awaiting_start[member] = at

    # Writing.

    def _progress(self) -> list[Effect]:
        """Do whatever has become possible: write what is owed, finish
        catching up, commit an induction."""
        effects = self._flush()
        if self.ledger.induction is Induction.CATCHING_UP and self._settled():
            self.ledger.starts = {key: r.next for key, r in self.readers.items()}
            self.ledger.induction = Induction.INDUCTING
        if (
            self.ledger.induction is Induction.INDUCTING
            and self.ledger.commit
            and self.pool
        ):
            effects += self._commit()
        return effects

    def _settled(self) -> bool:
        return not (self.unnumbered or self.outbox) and all(
            reader.mode is Mode.IDLE for reader in self.readers.values()
        )

    def _flush(self) -> list[Effect]:
        effects: list[Effect] = []
        while self.pool:
            if self._owes_control():
                effects += self._write_control()
            elif self.outbox:
                index = self.pool.pop(0)
                effects += self._emit(index, self.outbox.pop(0))
            else:
                break
        if len(self.pool) < 2 and not self.deriving:
            self.deriving = True
            effects.append(DeriveIndexes("own", self.me, self.tip, 1, POOL))
        return effects

    def _owes_control(self) -> bool:
        if self.ledger.induction is Induction.INDUCTING:
            return False
        return self.introduced_at is not None or any(
            self.rosters.index_of(self.me, introducer) is not None
            for introducer, _ in self.unnumbered
        )

    def _write_control(self) -> list[Effect]:
        """Number every member whose ``Introduction`` we have read, each
        after its introducer, and state where we were introduced."""
        index = self.pool.pop(0)
        at = position(index)
        numbers: list[tuple[int, IntroRef]] = []
        size = self.rosters.size(self.me)
        for introducer, intro_at in list(self.unnumbered):
            place = self.rosters.index_of(self.me, introducer)
            if place is None:
                continue
            self.unnumbered.remove((introducer, intro_at))
            self.rosters.number(self.me, size, (introducer, intro_at), at)
            numbers.append((size, IntroRef(place, intro_at)))
            size += 1
        message = Message(numbers=tuple(numbers), introduced_at=self.introduced_at)
        self.introduced_at = None
        return self._emit(index, message)

    def _emit(self, index: bytes, message: Message) -> list[Effect]:
        if message.introduction is None:
            message = Message(
                message.content, None, message.numbers, message.introduced_at, self._acks()
            )
        plaintext = self.codec.encode(message)
        self.sent.record(position(index), index, plaintext, message.control, self.epoch)
        return [WriteBox(index, plaintext)]

    def _acks(self) -> dict[int, bytes]:
        acks: dict[int, bytes] = {}
        for index, member in self.rosters.known(self.me).items():
            reader = self.readers.get(member) if member else None
            if reader is None:
                continue
            owed = self.ledger.owed(reader.stream, reader.last)
            if owed is not None:
                acks[index] = owed
                self.ledger.publish(reader.stream, owed)
        return acks

    def _derived(self, purpose: str, stream: Key, indexes: tuple[bytes, ...]) -> list[Effect]:
        if purpose == "own":
            self.deriving = False
            self.pool.extend(indexes)
            if indexes:
                self.tip = indexes[-1]
            return self._progress()
        reader = self.readers.get(stream)
        return reader.jumped(indexes) if reader else []

    # Inducting.

    def _invite(self, name: str, read_cap: bytes, commit: bool) -> list[Effect]:
        self.ledger.induction = Induction.CATCHING_UP
        self.ledger.invitee = (name, read_cap)
        self.ledger.commit = commit
        effects: list[Effect] = []
        for reader in self.readers.values():
            effects += reader.read()
        return effects + self._progress()

    def _commit(self) -> list[Effect]:
        assert self.ledger.invitee is not None
        name, read_cap = self.ledger.invitee
        new = key_of(read_cap)
        index = self.pool.pop(0)
        at = position(index)
        numbers: tuple[tuple[int, IntroRef], ...] = ()
        if self.rosters.index_of(self.me, new) is None:
            size = self.rosters.size(self.me)
            mine = self.rosters.index_of(self.me, self.me)
            assert mine is not None
            self.rosters.number(self.me, size, (self.me, at), at)
            numbers = ((size, IntroRef(mine, at)),)
        self.rosters.introductions[self.me, at] = new
        self.rosters.started.setdefault(new, (self.me, at))
        effects = self._emit(
            index, Message(introduction=Introduction(name, read_cap), numbers=numbers)
        )
        reply = self._reply(new, index, read_cap)
        if new not in self.readers:
            self._follow(read_cap, read_cap[KEY_SIZE:])
        self.ledger.induction = Induction.IDLE
        self.ledger.invitee = None
        self.ledger.commit = False
        self.ledger.starts = {}
        return effects + [SendJoinReply(read_cap, reply)] + self._flush()

    def _reply(self, new: Key, introduction: bytes, read_cap: bytes) -> JoinReply:
        order = self.rosters.known(self.me)
        size = max(order) + 1
        place = {key: index for index, key in order.items() if key is not None}
        members: list[tuple[bytes, bytes] | None] = []
        for index in range(size):
            key = order.get(index)
            if key == self.me:
                members.append((self.read_cap, introduction))
            elif key == new:
                members.append((read_cap, read_cap[KEY_SIZE:]))
            elif key is not None and key in self.readers:
                reader = self.readers[key]
                members.append((reader.read_cap, self.ledger.starts.get(key, reader.next)))
            else:
                members.append(None)
        rosters = []
        for index in range(size):
            key = order.get(index)
            numbered = self.rosters.numbered(key) if key is not None else {}
            rosters.append(
                tuple(
                    (i, place.get(k) if k else None, at)
                    for i, (k, at) in numbered.items()
                )
                or None
            )
        return JoinReply(
            members=tuple(members),
            rosters=tuple(rosters),
            introductions=tuple(
                sorted(
                    (place[i], at, place[k])
                    for (i, at), k in self.rosters.introductions.items()
                    if i in place and k in place
                )
            ),
            introducer=place[self.me],
            introduction=position(introduction),
        )

    # Membership and time.

    def _remove(self, member: Key) -> None:
        place = self.rosters.index_of(self.me, member)
        token = placeholder(place) if place is not None else b"retired:" + member[:8]
        self.rosters.retire(member, token)
        self.readers.pop(member, None)
        self.ledger.acked_by.pop(member, None)
        self.ledger.heard.pop(member, None)
        self.unnumbered = [
            intro for intro in self.unnumbered
            if self.rosters.introductions.get(intro) != token
        ]

    def _epoch(self, epoch: int) -> list[Effect]:
        self.epoch = epoch
        self.sent.retain(epoch, self.policy, self._everyone_acked)
        effects: list[Effect] = []
        keep = self.policy.keep_received
        if keep is not None:
            for (sender, at), received in list(self.received.items()):
                if epoch - received >= keep:
                    del self.received[sender, at]
                    effects.append(Expire(sender, at))
        return effects + self.sent.backfill(epoch)

    def _everyone_acked(self, at: int) -> bool:
        return all(self.ledger.acked_by.get(key, -1) >= at for key in self.readers)
