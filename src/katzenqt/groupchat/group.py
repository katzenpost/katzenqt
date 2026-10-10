"""One member of one group chat, as a sans-io state machine. The parts each
own one concern: a :class:`~.reader.Reader` per stream, the
:class:`~.sentbox.SentBox`, the :class:`~.ledger.Ledger` of
acknowledgements and the :class:`~.numbering.Rosters`.

It can be driven in two ways. :meth:`Group.handle` takes an
:mod:`~katzenqt.groupchat.events` event and returns the effects to carry
out, with the machine choosing every box to read and write. A client that
does its own reading and writing instead tells it what it read
(:meth:`Group.observe`), asks it what to send (:meth:`Group.compose`), and
tells it where each message landed (:meth:`Group.written`). Either way a
message changes rosters only once it has a place on our stream.
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
    THIS_MESSAGE,
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
class Pending:
    """What a message we have composed does once it has a place."""

    numbers: tuple[tuple[int, At], ...] = ()
    """Our roster indexes it assigns, and to whose ``Introduction``."""
    introduced: Key | None = None
    acks: dict[Key, tuple[bytes | None, bytes]] = field(default_factory=dict)
    """Each stream it acknowledges: what we had published before, and now."""
    control: bool = False


@dataclass(frozen=True)
class Outgoing:
    token: int
    message: Message


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
    outbox: list[bytes] = field(default_factory=list)
    """Content waiting for an index."""
    pending: dict[int, Pending] = field(default_factory=dict)
    """Messages composed that have no place yet, by token."""
    tokens: int = 0
    unnumbered: list[At] = field(default_factory=list)
    """Introductions read whose member we have not numbered yet."""
    introduced_at: IntroRef | None = None
    """Owed in our next message, once we have read our ``Introduction``."""
    joined_through: Key | None = None
    """Our introducer, until we have read our ``Introduction``."""
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
        ] + [group.me]
        for place, roster in enumerate(reply.rosters):
            if roster is not None:
                group.rosters.entries[order[place]] = {
                    index: Entry(order[slot] if slot is not None else None, at=at)
                    for index, slot, at in roster
                }
        for by, at, member in reply.introductions:
            group.rosters.introductions[order[by], at] = order[member]
        introducer = order[reply.introducer]
        group.rosters.entries[group.me] = {
            index: Entry(entry.key, entry.introduced)
            for index, entry in group.rosters.entries.get(introducer, {}).items()
        }
        group.rosters.entries[group.me][reply.newcomer] = Entry(group.me)
        for slot in reply.members:
            if slot:
                group._follow(*slot)
        group.joined_through = introducer
        return group

    # Driven by events.

    def handle(self, event: Event) -> list[Effect]:
        match event:
            case Send(content):
                self.outbox.append(content)
                return self._progress()
            case Read(stream) if stream in self.readers:
                return self.readers[stream].read()
            case Scan(stream) if stream in self.readers:
                return self._scan(stream)
            case BoxRead():
                return self._box_read(event) + self._progress()
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
                self.remove(member)
                return []
            case EpochChanged(epoch):
                return self._epoch(epoch)
        return []

    # Driven by a client that reads and writes for itself.

    def observe(
        self, stream: Key, index: bytes, plaintext: bytes | None, next_index: bytes
    ) -> list[Effect]:
        """A box read on ``stream``, in order: its plaintext, or None for one
        that holds no message we can use."""
        reader = self.readers.get(stream)
        if reader is None:
            return []
        reader.next = index
        reader.mode = Mode.READING
        effects = self._box_read(BoxRead(stream, index, plaintext or b"", next_index))
        reader.mode = Mode.IDLE
        return [e for e in effects if isinstance(e, Deliver | Expire)]

    def compose(self, content: bytes | None = None) -> list[Outgoing]:
        """The messages to send now, in order: a control message if one is
        owed, then ``content``. Each takes effect when :meth:`written`."""
        staged = [self._control()] if self._owes_control() else []
        if content is not None:
            staged.append((Message(content=content), Pending()))
        return [self._stage(self._with_acks(m, p), p) for m, p in staged]

    def written(self, token: int, index: bytes, plaintext: bytes | None = None) -> None:
        """The message ``token`` landed at ``index`` on our stream."""
        pending = self.pending.pop(token, None)
        if pending is None:
            return
        at = position(index)
        for number, (introducer, intro_at) in pending.numbers:
            if intro_at == THIS_MESSAGE:
                intro_at = at
            self.rosters.number(self.me, number, (introducer, intro_at), at)
        if pending.introduced is not None:
            self.rosters.introductions[self.me, at] = pending.introduced
            self.rosters.started.setdefault(pending.introduced, (self.me, at))
        self.sent.record(at, index, plaintext, pending.control, self.epoch)

    def abandoned(self, token: int) -> None:
        """The message ``token`` will never be written: whatever it
        acknowledged is owed again, and whoever it numbered still needs
        numbering."""
        pending = self.pending.pop(token, None)
        if pending is None:
            return
        for stream, (before, sent) in pending.acks.items():
            if self.ledger.published.get(stream) == sent:
                if before is None:
                    del self.ledger.published[stream]
                else:
                    self.ledger.published[stream] = before
        if pending.introduced is None:
            self.unnumbered.extend(intro for _, intro in pending.numbers)

    def begin_induction(self) -> list[Outgoing]:
        """Begin inducting a member, starting each stream where we have read
        it now. Anything owed goes out first; send it before :meth:`reply`."""
        outgoing = self.compose()
        self._hold()
        return outgoing

    def introduce_now(self, name: str, read_cap: bytes) -> Outgoing:
        """Commit the induction under way: the ``Introduction`` of the
        member read by ``read_cap``, to send."""
        message, pending = self._introduction(name, read_cap)
        return self._stage(message, pending)

    def cancel_induction(self) -> None:
        self._release()

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
        return effects

    def _ingest(self, sender: Key, at: int, plaintext: bytes) -> list[Effect]:
        message = self.codec.decode(plaintext)
        if message is None:
            return []
        for index, ref in message.numbers:
            introducer = self.rosters.resolve(sender, ref.introducer)
            intro_at = at if ref.position == THIS_MESSAGE else ref.position
            self.rosters.number(
                sender, index, (introducer, intro_at) if introducer else None, at
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
        if new == self.me and introducer == self.joined_through:
            place = self.rosters.index_of(self.me, introducer)
            assert place is not None
            self.introduced_at = IntroRef(place, at)
            self.rosters.started[self.me] = (introducer, at)
            self.joined_through = None
        if self.awaiting_start.get(new) == at:
            del self.awaiting_start[new]
            self.rosters.started[new] = (introducer, at)
        if new == self.me or self._numbered(new):
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

    def _numbered(self, member: Key) -> bool:
        """Whether ``member`` is in our roster, or will be once what we have
        composed is written."""
        if self.rosters.index_of(self.me, member) is not None:
            return True
        return any(
            pending.introduced == member
            or any(self.rosters.introductions.get(intro) == member for _, intro in pending.numbers)
            for pending in self.pending.values()
        )

    def _size(self) -> int:
        """The next index of our roster, counting what is composed."""
        reserved = [n + 1 for p in self.pending.values() for n, _ in p.numbers]
        return max([self.rosters.size(self.me), *reserved])

    def _owes_control(self) -> bool:
        if self.ledger.holding:
            return False
        return self.introduced_at is not None or any(
            self.rosters.index_of(self.me, introducer) is not None
            for introducer, _ in self.unnumbered
        )

    def _control(self) -> tuple[Message, Pending]:
        """Number every member whose ``Introduction`` we have read and whose
        introducer we have numbered, and say where we were introduced."""
        numbers: list[tuple[int, IntroRef]] = []
        assigned: list[tuple[int, At]] = []
        size = self._size()
        for introducer, intro_at in list(self.unnumbered):
            place = self.rosters.index_of(self.me, introducer)
            if place is None:
                continue
            self.unnumbered.remove((introducer, intro_at))
            numbers.append((size, IntroRef(place, intro_at)))
            assigned.append((size, (introducer, intro_at)))
            size += 1
        message = Message(numbers=tuple(numbers), introduced_at=self.introduced_at)
        self.introduced_at = None
        return message, Pending(numbers=tuple(assigned), control=True)

    def _introduction(self, name: str, read_cap: bytes) -> tuple[Message, Pending]:
        new = key_of(read_cap)
        numbers: tuple[tuple[int, IntroRef], ...] = ()
        assigned: tuple[tuple[int, At], ...] = ()
        if not self._numbered(new):
            size = self._size()
            mine = self.rosters.index_of(self.me, self.me)
            assert mine is not None
            numbers = ((size, IntroRef(mine, THIS_MESSAGE)),)
            assigned = ((size, (self.me, THIS_MESSAGE)),)
        if new not in self.readers:
            self._follow(read_cap, read_cap[KEY_SIZE:])
        self._release()
        message = Message(introduction=Introduction(name, read_cap), numbers=numbers)
        return message, Pending(numbers=assigned, introduced=new, control=True)

    def _with_acks(self, message: Message, pending: Pending) -> Message:
        acks: dict[int, bytes] = {}
        for index, member in self.rosters.known(self.me).items():
            reader = self.readers.get(member) if member else None
            if reader is None:
                continue
            owed = self.ledger.owed(reader.stream, reader.last)
            if owed is not None:
                acks[index] = owed
                pending.acks[reader.stream] = (self.ledger.published.get(reader.stream), owed)
                self.ledger.publish(reader.stream, owed)
        return Message(
            message.content, message.introduction, message.numbers, message.introduced_at, acks
        )

    def _stage(self, message: Message, pending: Pending) -> Outgoing:
        self.tokens += 1
        self.pending[self.tokens] = pending
        return Outgoing(self.tokens, message)

    def _write(self, outgoing: Outgoing) -> list[Effect]:
        index = self.pool.pop(0)
        plaintext = self.codec.encode(outgoing.message)
        self.written(outgoing.token, index, plaintext)
        return [WriteBox(index, plaintext)]

    def _progress(self) -> list[Effect]:
        """Do whatever has become possible: write what is owed, finish
        catching up, commit an induction."""
        effects = self._flush()
        if self.ledger.induction is Induction.CATCHING_UP and self._settled():
            self._hold()
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
        while self.pool and (self._owes_control() or self.outbox):
            if self._owes_control():
                message, pending = self._control()
            else:
                message, pending = Message(content=self.outbox.pop(0)), Pending()
            effects += self._write(self._stage(self._with_acks(message, pending), pending))
        if len(self.pool) < 2 and not self.deriving:
            self.deriving = True
            effects.append(DeriveIndexes("own", self.me, self.tip, 1, POOL))
        return effects

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

    def _hold(self) -> None:
        self.ledger.induction = Induction.INDUCTING
        self.ledger.starts = {key: r.next for key, r in self.readers.items()}

    def _release(self) -> None:
        self.ledger.induction = Induction.IDLE
        self.ledger.invitee = None
        self.ledger.commit = False
        self.ledger.starts = {}

    def _commit(self) -> list[Effect]:
        assert self.ledger.invitee is not None
        name, read_cap = self.ledger.invitee
        reply = self.reply(self.pool[0])
        effects = self._write(self.introduce_now(name, read_cap))
        return effects + [SendJoinReply(read_cap, reply)] + self._flush()

    def reply(self, own_start: bytes) -> JoinReply:
        """What to hand a new member, which will start our stream at
        ``own_start``. Call it while inducting, before the ``Introduction``."""
        order = self.rosters.known(self.me)
        size = max(order) + 1
        place = {key: index for index, key in order.items() if key is not None}
        members: list[tuple[bytes, bytes] | None] = []
        for index in range(size):
            key = order.get(index)
            if key == self.me:
                members.append((self.read_cap, own_start))
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
                tuple((i, place.get(k) if k else None, at) for i, (k, at) in numbered.items())
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
            newcomer=size,
        )

    # Membership and time.

    def remove(self, member: Key) -> None:
        """Stop reading ``member`` and forget its key, keeping its places."""
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
