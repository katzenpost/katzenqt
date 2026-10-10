"""One group member as a sans-io state machine: :meth:`Member.handle` takes
an event and returns the effects the driver must carry out. Boxes are real
BACAP boxes; the driver only stores and fetches them. It follows the group
chat spec as written, so that scenarios can show where the spec goes wrong;
:class:`Settings` switches on proposed fixes.

Positions are a box's BACAP ``Idx64``, the counter its ``MessageBoxIndex``
begins with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

import cbor2
from hpqc.bacap import (
    BACAPError,
    MessageBoxIndex,
    ReadCap,
    ReadPosition,
    WriteCap,
    WritePosition,
)

from .. import ack_codec, rosters

Key = rosters.Member
"""A member's read-cap public key."""

CTX = b"pigeonhole context"


def key_of(cap: ReadCap) -> Key:
    return bytes(cap.root_public_key)


@dataclass(frozen=True)
class Message:
    text: str | None = None
    introduction: tuple[str, bytes] | None = None
    """Display name and ``ReadCap`` bytes."""
    acks: dict[int, bytes] = field(default_factory=dict)
    """Roster index to ``MessageBoxIndex`` bytes."""

    def encode(self) -> bytes:
        gcm: dict[str, object] = {"Version": 1}
        if self.text is not None:
            gcm["TextPayload"] = {"Payload": self.text.encode()}
        if self.introduction is not None:
            name, cap = self.introduction
            gcm["Introduction"] = {"DisplayName": name, "UniversalReadCap": cap}
        if self.acks:
            gcm["Acks"] = ack_codec.encode(self.acks)
        return cbor2.dumps(gcm)

    @classmethod
    def decode(cls, plaintext: bytes) -> Message:
        gcm = cbor2.loads(plaintext)
        text = gcm.get("TextPayload")
        intro = gcm.get("Introduction")
        try:
            acks = ack_codec.decode(gcm.get("Acks") or b"")
        except ack_codec.AcksError:
            acks = {}
        return cls(
            text["Payload"].decode() if text else None,
            (intro["DisplayName"], intro["UniversalReadCap"]) if intro else None,
            acks,
        )


@dataclass(frozen=True)
class Settings:
    window: int = 3
    """Epochs a Sent-box record is kept and backfilled."""
    redact: bool = False
    """Proposed fix: backfill writes a message whose plaintext is gone as a
    message keeping only its Acks and Introduction, not as a tombstone."""
    ack_jump: bool = False
    """Proposed fix: a scan jumps to the furthest box any member has
    acknowledged on the stream."""
    recheck: int = 4
    """How many positions behind its cursor a reader keeps the index of a
    box it passed without data, so a scan can fetch it again."""
    discard_acked: bool = True
    """Drop a sent message's plaintext once every member we read has
    acknowledged it."""
    keep_sent: int | None = None
    """Epochs the retention policy keeps a sent message's plaintext."""
    keep_received: int | None = None
    """Epochs the retention policy keeps a received message."""


# Events.


@dataclass(frozen=True)
class Read:
    owner: Key


@dataclass(frozen=True)
class ScanRequested:
    owner: Key


@dataclass(frozen=True)
class Stored:
    """A box as a replica returns it. An empty payload is a tombstone."""

    box_id: bytes
    payload: bytes
    signature: bytes


@dataclass(frozen=True)
class Found:
    """The result of a fetch: ``stored`` is None for ``BoxIDNotFound``."""

    owner: Key
    position: int
    stored: Stored | None


@dataclass(frozen=True)
class Send:
    text: str


@dataclass(frozen=True)
class Invite:
    """An induction begins: no acks are sent until it is committed."""

    name: str
    cap: ReadCap


@dataclass(frozen=True)
class Introduce:
    """Commit an induction: write the ``Introduction`` and hand over."""

    name: str
    cap: ReadCap


@dataclass(frozen=True)
class Remove:
    member: Key


@dataclass(frozen=True)
class Joined:
    order: tuple[Key, ...]
    caps: tuple[bytes, ...]
    """Each member's ``ReadCap``, rebased to where the new member starts."""
    handover: rosters.Handover


@dataclass(frozen=True)
class EpochChanged:
    epoch: int


@dataclass(frozen=True)
class Purge:
    """The retention policy deletes the plaintext of every message sent."""


Event: TypeAlias = (
    Read | ScanRequested | Found | Send | Invite | Introduce | Remove
    | Joined | EpochChanged | Purge
)


# Effects.


@dataclass(frozen=True)
class Write:
    """Store a box. An empty payload is a tombstone."""

    position: int
    box_id: bytes
    payload: bytes
    signature: bytes


@dataclass(frozen=True)
class Fetch:
    owner: Key
    position: int
    box_id: bytes
    recheck: bool = False


@dataclass(frozen=True)
class Show:
    """A message read or sent, for the user; None for a tombstone."""

    owner: Key
    position: int
    message: Message | None


@dataclass(frozen=True)
class HandOver:
    to: Key
    joined: Joined


Effect: TypeAlias = Write | Fetch | Show | HandOver


@dataclass
class Cursor:
    origin: ReadCap
    """Where this member could first read the stream."""
    next: ReadPosition
    last: MessageBoxIndex | None = None
    acked: MessageBoxIndex | None = None
    """The furthest box our own published acks have reached."""
    missed: dict[int, ReadPosition] = field(default_factory=dict)
    """Recent positions passed without data, kept for a scan to recheck."""


@dataclass
class Record:
    at: WritePosition
    plaintext: bytes | None
    created: int
    control: Message


@dataclass
class Heard:
    """The furthest box some member has acknowledged on a stream."""

    index: MessageBoxIndex
    by: Key


def _pos(index: MessageBoxIndex) -> int:
    return index.idx_64


class Member:
    def __init__(
        self, name: str, cap: WriteCap, settings: Settings = Settings()
    ) -> None:
        self.name = name
        self.cap = cap
        self.key = key_of(cap.read_cap())
        self.settings = settings
        self.group = rosters.Group()
        self.cursors: dict[Key, Cursor] = {}
        self.next = cap.start()
        self.records: dict[int, Record] = {}
        self.epoch = 0
        self.backfilled = -1
        self.acked_by: dict[Key, int] = {}
        self.heard: dict[Key, Heard] = {}
        self.ack_values: dict[tuple[Key, int], dict[int, bytes]] = {}
        self.inducting: set[Key] = set()
        self.inbox: dict[tuple[Key, int], tuple[int, str]] = {}
        """Received messages: their epoch and text."""

    @property
    def read_cap(self) -> ReadCap:
        return self.cap.read_cap()

    def found(self, founders: list[ReadCap]) -> None:
        """Start the group with ``founders``, ourselves among them."""
        caps = {key_of(cap): cap for cap in founders}
        roster = tuple(sorted(caps))
        for other, cap in caps.items():
            self.group.bases[other] = roster
            if other != self.key:
                self.cursors[other] = Cursor(cap, cap.start())

    def handle(self, event: Event) -> list[Effect]:
        match event:
            case Read(owner) if owner in self.cursors:
                return [self._fetch(owner)]
            case ScanRequested(owner) if owner in self.cursors:
                return self._scan(owner)
            case Read() | ScanRequested():
                return []
            case Found(owner, position, stored):
                return self._found(owner, position, stored)
            case Send(text):
                return self._write(Message(text, acks=self._take_acks()))
            case Invite(_, cap):
                self.inducting.add(key_of(cap))
                return []
            case Introduce(name, cap):
                self.inducting.discard(key_of(cap))
                return self._introduce(name, cap)
            case Remove(member):
                self._remove(member)
                return []
            case Joined():
                self._join(event)
                return []
            case EpochChanged(epoch):
                self.epoch = epoch
                self._retain(epoch)
                return self._backfill(epoch)
            case Purge():
                for record in self.records.values():
                    record.plaintext = None
                return []
        raise TypeError(f"unhandled event {event!r}")

    def roster(self, owner: Key) -> rosters.Followed | None:
        return rosters.follow(self.group, owner)

    def resolve(
        self, owner: Key, position: int
    ) -> dict[Key, MessageBoxIndex]:
        """Whom ``owner``'s message at ``position`` acknowledges, as this
        member resolves it: against ``owner``'s roster before that message."""
        values = self.ack_values.get((owner, position), {})
        before = rosters.follow(self.group, owner, position - 1)
        if before is None:
            return {}
        return {
            before.roster[index]: MessageBoxIndex.from_bytes(value)
            for index, value in sorted(values.items())
            if index < len(before.roster)
        }

    def on_stream(self, member: Key, index: MessageBoxIndex) -> bool:
        """Whether ``index`` is a box of ``member``'s stream that this
        member can read."""
        cap = (
            self.read_cap
            if member == self.key
            else self.cursors[member].origin
            if member in self.cursors
            else None
        )
        if cap is None:
            return False
        try:
            cap.contains(index)
        except BACAPError:
            return False
        return True

    def _fetch(self, owner: Key) -> Fetch:
        at = self.cursors[owner].next
        return Fetch(owner, _pos(at.index), at.box_id(CTX))

    def _scan(self, owner: Key) -> list[Effect]:
        cursor = self.cursors[owner]
        heard = self.heard.get(owner)
        if (
            self.settings.ack_jump
            and heard is not None
            and _pos(heard.index) >= _pos(cursor.next.index)
        ):
            self._skip_to(cursor, _pos(heard.index))
        rechecks: list[Effect] = [
            Fetch(owner, position, at.box_id(CTX), recheck=True)
            for position, at in sorted(cursor.missed.items())
        ]
        return rechecks + [self._fetch(owner)]

    def _skip_to(self, cursor: Cursor, target: int) -> None:
        """Move the cursor to ``target``, keeping the indexes of the last
        positions skipped. BACAP indexes only advance, so they are derived
        now, on the way past."""
        first = max(_pos(cursor.next.index), target - self.settings.recheck)
        at = cursor.next.advance_to(first)
        while _pos(at.index) < target:
            cursor.missed[_pos(at.index)] = at
            at = at.next()
        cursor.next = at

    def _found(
        self, owner: Key, position: int, stored: Stored | None
    ) -> list[Effect]:
        cursor = self.cursors.get(owner)
        if cursor is None or stored is None:
            return []
        if position in cursor.missed:
            return self._rechecked(owner, cursor, position, stored)
        if position != _pos(cursor.next.index):
            return []
        at = cursor.next
        message = self._open(at, stored)
        self._take(owner, position, message)
        if message is None:
            cursor.missed[position] = at
        cursor.last = at.index
        cursor.next = at.next()
        horizon = _pos(cursor.next.index) - self.settings.recheck
        for old in [p for p in cursor.missed if p < horizon]:
            del cursor.missed[old]
        return [Show(owner, position, message), self._fetch(owner)]

    def _rechecked(
        self, owner: Key, cursor: Cursor, position: int, stored: Stored
    ) -> list[Effect]:
        message = self._open(cursor.missed[position], stored)
        if message is None:
            return []
        del cursor.missed[position]
        self._take(owner, position, message)
        return [Show(owner, position, message)]

    def _open(self, at: ReadPosition, stored: Stored) -> Message | None:
        plaintext = at.open(
            CTX, stored.box_id, stored.payload, stored.signature
        )
        return Message.decode(plaintext) if plaintext else None

    def _take(self, owner: Key, position: int, message: Message | None) -> None:
        if message is None:
            self._saw(owner, position)
            return
        self._ingest(owner, position, message)
        if message.text is not None:
            self.inbox[owner, position] = (self.epoch, message.text)

    def _saw(self, owner: Key, position: int) -> None:
        self.group.seen[owner] = max(self.group.seen.get(owner, -1), position)

    def _ingest(self, owner: Key, position: int, message: Message) -> None:
        group = self.group
        self._saw(owner, position)
        if message.acks:
            self.ack_values[owner, position] = message.acks
            group.acks[owner, position] = {
                index: _pos(MessageBoxIndex.from_bytes(value))
                for index, value in message.acks.items()
            }
            before = rosters.follow(group, owner, position - 1)
            for index, value in message.acks.items():
                if before is not None and index < len(before.roster):
                    self._acknowledged(owner, before.roster[index], value)
        if message.introduction is not None:
            cap = ReadCap.from_bytes(message.introduction[1])
            new = key_of(cap)
            group.introductions[owner, position] = new
            group.bases.setdefault(new, rosters.Inherited(owner, position))
            if new != self.key and new not in self.cursors:
                self.cursors[new] = Cursor(cap, cap.start())

    def _acknowledged(self, acker: Key, member: Key, value: bytes) -> None:
        index = MessageBoxIndex.from_bytes(value)
        if not self.on_stream(member, index):
            return
        heard = self.heard.get(member)
        if heard is None or _pos(index) > _pos(heard.index):
            self.heard[member] = Heard(index, acker)
        record = self.records.get(_pos(index))
        if member == self.key and record and record.at.index == index:
            self.acked_by[acker] = max(
                self.acked_by.get(acker, -1), _pos(index)
            )

    def _take_acks(self) -> dict[int, bytes]:
        if self.inducting:
            return {}
        roster = rosters.roster_of(self.group, self.key) or ()
        acks: dict[int, bytes] = {}
        for index, member in enumerate(roster):
            cursor = self.cursors.get(member)
            if cursor is None or cursor.last is None or cursor.last == cursor.acked:
                continue
            cursor.acked = cursor.last
            acks[index] = cursor.last.to_bytes()
        return acks

    def _write(self, message: Message) -> list[Effect]:
        at = self.next
        self.next = at.next()
        position = _pos(at.index)
        plaintext = message.encode()
        self.records[position] = Record(
            at,
            plaintext,
            self.epoch,
            Message(introduction=message.introduction, acks=message.acks),
        )
        self._ingest(self.key, position, message)
        return [self._seal(at, plaintext), Show(self.key, position, message)]

    def _seal(self, at: WritePosition, plaintext: bytes) -> Write:
        box_id, ciphertext, signature = at.encrypt(CTX, plaintext)
        return Write(_pos(at.index), box_id, ciphertext, signature)

    def _introduce(self, name: str, cap: ReadCap) -> list[Effect]:
        new = key_of(cap)
        effects = self._write(Message(introduction=(name, cap.to_bytes())))
        at = self.records[max(self.records)].at.index
        roster = rosters.roster_of(self.group, self.key) or ()
        order = tuple(roster) + tuple(
            member for member in self.cursors if member not in roster
        )
        caps: list[ReadCap | None] = []
        for member in order:
            if member == self.key:
                caps.append(self.read_cap.with_message_box_index(at))
            elif member == new:
                caps.append(cap)
            elif member not in self.cursors:
                caps.append(None)
            else:
                cursor = self.cursors[member]
                start = (
                    cursor.acked.next_index()
                    if cursor.acked is not None
                    else cursor.origin.message_box_index
                )
                caps.append(cursor.origin.with_message_box_index(start))
        joined = Joined(
            order,
            tuple(c.to_bytes() if c else b"" for c in caps),
            rosters.hand_over(self.group, order),
        )
        return effects + [HandOver(new, joined)]

    def _join(self, joined: Joined) -> None:
        """A removed member's slot comes empty: it is known only by its
        place."""
        order = tuple(
            member if data else rosters.placeholder(place)
            for place, (member, data) in enumerate(zip(joined.order, joined.caps))
        )
        self.group = rosters.adopt(order, joined.handover)
        for member, data in zip(order, joined.caps):
            if data and member != self.key:
                cap = ReadCap.from_bytes(data)
                self.cursors[member] = Cursor(cap, cap.start())

    def _remove(self, member: Key) -> None:
        """Stop reading ``member`` and forget its key, keeping every place
        it holds in the rosters."""
        roster = rosters.roster_of(self.group, self.key) or ()
        token = (
            rosters.placeholder(roster.index(member))
            if member in roster
            else b"retired:" + member[:8]
        )
        rosters.retire(self.group, member, token)
        for table in (self.cursors, self.acked_by, self.heard):
            table.pop(member, None)
        self.inducting.discard(member)

    def _retain(self, epoch: int) -> None:
        """Apply the retention policy."""
        settings = self.settings
        for position, record in self.records.items():
            if (
                settings.keep_sent is not None
                and epoch - record.created >= settings.keep_sent
            ) or (settings.discard_acked and self._everyone_acked(position)):
                record.plaintext = None
        if settings.keep_received is not None:
            for at, (received, _) in list(self.inbox.items()):
                if epoch - received >= settings.keep_received:
                    del self.inbox[at]

    def _everyone_acked(self, position: int) -> bool:
        return all(
            self.acked_by.get(member, -1) >= position
            for member in self.cursors
        )

    def _backfill(self, epoch: int) -> list[Effect]:
        if epoch == self.backfilled:
            return []
        self.backfilled = epoch
        for position in list(self.records):
            if epoch - self.records[position].created > self.settings.window:
                del self.records[position]
        effects: list[Effect] = []
        for record in (self.records[p] for p in sorted(self.records)):
            if record.plaintext is not None:
                effects.append(self._seal(record.at, record.plaintext))
            elif self.settings.redact:
                effects.append(self._seal(record.at, record.control.encode()))
            else:
                box_id, signature = record.at.tombstone(CTX)
                effects.append(
                    Write(_pos(record.at.index), box_id, b"", signature)
                )
        return effects
