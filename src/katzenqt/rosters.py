"""Rosters: how each member of a group numbers the others, and how everyone
else works that numbering out.

Pure: plain data in, plain data out; no session, no connection. See
"Rosters" in the group chat protocol spec.

A member is known here by its read-cap public key. Every member has a
roster, an ordered list of the members it has numbered, and its roster
index for a member is that member's place in the list. Nobody ever states
a roster index. A roster grows in two ways, both visible on its owner's
stream: the owner introduces a new member, or the owner sends a message
whose acknowledgement of some stream reaches an ``Introduction`` on that
stream. So two kinds of fact are enough to rebuild anyone's roster: every
``Introduction`` read, and every acknowledgement read. :func:`follow` does
the rebuilding, from scratch each time, in the order of the owner's
stream, not the order of arrival, since a message sent through a substream
arrives late.

A watcher may be behind. A member can acknowledge further along a stream
than the watcher has read, and the watcher then cannot tell whom that
member numbered there. :func:`follow` stops at the first such message and
returns the roster up to it, which is exact as far as it goes because a
roster only grows. The messages it could not apply are returned too, so
they can be handed to a new member along with the roster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

Member = bytes
"""A member's read-cap public key, or the placeholder left where a removed
member has been forgotten."""

Roster = tuple[Member, ...]

Levels = Mapping[int, int]
"""One message's acknowledgements: the sender's roster index for a member,
and the position reached on that member's stream."""

ROSTER_MAX = 256
"""The most entries a roster holds."""


def placeholder(place: int) -> Member:
    """What stands in a roster for a member no longer known by key. It is
    not the length of a key, so it equals no real member.

    >>> placeholder(3)
    b'retired\\x03'
    """
    return b"retired" + bytes([place])


@dataclass(frozen=True)
class Inherited:
    """A roster that began as a copy of ``introducer``'s as it stood at the
    ``Introduction`` it wrote at ``position``."""

    introducer: Member
    position: int


@dataclass
class Group:
    """What one client knows of a group's rosters.

    ``bases``: where each roster began. ``seen``: for each stream, the
    position through which this client has every message. ``introductions``:
    the member introduced at a position on an introducer's stream.
    ``acks``: the acknowledgements a sender wrote at a position on its own
    stream.
    """

    bases: dict[Member, Roster | Inherited] = field(default_factory=dict)
    seen: dict[Member, int] = field(default_factory=dict)
    introductions: dict[tuple[Member, int], Member] = field(
        default_factory=dict
    )
    acks: dict[tuple[Member, int], Levels] = field(default_factory=dict)


class Followed(NamedTuple):
    """A roster as far as it can be told, and the acknowledgements of its
    owner that could not yet be applied to it, by position."""

    roster: Roster
    unsettled: tuple[tuple[int, Levels], ...]


def grow(
    roster: Sequence[Member],
    levels: Levels,
    introductions: Mapping[tuple[Member, int], Member],
) -> list[Member]:
    """The members one message numbers through its acknowledgements, in
    order: by the introducer's place in the sender's roster, then by
    position on the introducer's stream. ``levels`` is read against
    ``roster`` as it stood before the message.

    >>> alice, bob, carol, dave = b"a", b"b", b"c", b"d"
    >>> known = {(bob, 4): carol, (bob, 9): dave}
    >>> grow([alice, bob], {1: 4}, known)
    [b'c']
    >>> grow([alice, bob], {1: 12}, known)
    [b'c', b'd']
    >>> grow([alice, bob, carol], {1: 12}, known)
    [b'd']
    >>> grow([alice, bob], {1: 3}, known)
    []
    """
    by_introducer: dict[Member, list[tuple[int, Member]]] = {}
    for (introducer, position), member in introductions.items():
        by_introducer.setdefault(introducer, []).append((position, member))
    added: list[Member] = []
    for index in sorted(levels):
        if not 0 <= index < len(roster):
            continue
        reached = levels[index]
        for position, member in sorted(by_introducer.get(roster[index], ())):
            if position > reached:
                break
            if member not in roster and member not in added:
                added.append(member)
    return added


def follow(
    group: Group, member: Member, through: int | None = None
) -> Followed | None:
    """``member``'s roster as far as this client can tell it, counting what
    it wrote up to position ``through``, or everything. None when this
    client cannot yet tell where that roster began.

    >>> alice, bob, carol = b"a", b"b", b"c"
    >>> group = Group(
    ...     bases={alice: (alice,), bob: Inherited(alice, 0)},
    ...     seen={alice: 3, bob: 5},
    ...     introductions={(alice, 0): bob, (bob, 5): carol},
    ...     acks={(alice, 3): {1: 7}},
    ... )
    >>> follow(group, bob).roster
    (b'a', b'b', b'c')
    >>> follow(group, alice)
    Followed(roster=(b'a', b'b'), unsettled=((3, {1: 7}),))
    >>> group.seen[bob] = 7
    >>> follow(group, alice)
    Followed(roster=(b'a', b'b', b'c'), unsettled=())
    >>> follow(group, carol) is None
    True
    """
    return _follow(group, member, through, frozenset())


def _follow(
    group: Group,
    member: Member,
    through: int | None,
    visiting: frozenset[Member],
) -> Followed | None:
    start = _start(group, member, visiting)
    if start is None:
        return None
    roster = list(start)
    unsettled: list[tuple[int, Levels]] = []
    have = group.seen.get(member, -1)
    for position, levels, introduced in _timeline(group, member):
        if through is not None and position > through:
            break
        if position > have:
            if levels is not None:
                unsettled.append((position, levels))
            continue
        if levels is not None:
            if unsettled or not _readable(group, roster, levels):
                unsettled.append((position, levels))
            else:
                roster.extend(grow(roster, levels, group.introductions))
        elif not unsettled and introduced not in roster:
            assert introduced is not None
            roster.append(introduced)
    return Followed(tuple(roster[:ROSTER_MAX]), tuple(unsettled))


def _start(
    group: Group, member: Member, visiting: frozenset[Member]
) -> Roster | None:
    base = group.bases.get(member)
    if base is None or member in visiting:
        return None
    if not isinstance(base, Inherited):
        return base
    if group.seen.get(base.introducer, -1) < base.position:
        return None
    inherited = _follow(
        group, base.introducer, base.position, visiting | {member}
    )
    if inherited is None or inherited.unsettled:
        return None
    return inherited.roster


def _timeline(
    group: Group, member: Member
) -> list[tuple[int, Levels | None, Member | None]]:
    """What ``member`` wrote that changes its roster, in stream order. A
    message that both acknowledges and introduces numbers the acknowledged
    members first."""
    steps: list[tuple[int, int, Levels | None, Member | None]] = [
        (position, 0, levels, None)
        for (sender, position), levels in group.acks.items()
        if sender == member
    ]
    steps.extend(
        (position, 1, None, introduced)
        for (introducer, position), introduced in group.introductions.items()
        if introducer == member
    )
    steps.sort(key=lambda step: step[:2])
    return [(position, levels, who) for position, _, levels, who in steps]


def _readable(group: Group, roster: Sequence[Member], levels: Levels) -> bool:
    """Whether this client has read as far as ``levels`` reaches on every
    stream it names, and so knows every ``Introduction`` it covers."""
    return all(
        0 <= index < len(roster)
        and reached <= group.seen.get(roster[index], -1)
        for index, reached in levels.items()
    )


def resolve(
    group: Group, sender: Member, levels: Levels
) -> dict[Member, int]:
    """Whom a message from ``sender`` acknowledges, and how far. A roster
    index this client cannot yet place is left out; because a roster only
    grows, the ones it can place are right whatever comes later.

    >>> alice, bob, carol = b"a", b"b", b"c"
    >>> group = Group(bases={alice: (alice, bob)}, seen={alice: 9})
    >>> resolve(group, alice, {1: 7, 2: 1})
    {b'b': 7}
    >>> resolve(group, carol, {0: 1})
    {}
    """
    followed = follow(group, sender)
    if followed is None:
        return {}
    return {
        followed.roster[index]: reached
        for index, reached in sorted(levels.items())
        if 0 <= index < len(followed.roster)
    }


@dataclass(frozen=True)
class Handover:
    """What an introducer tells a new member about the group, with every
    member written as its place in the list of members handed over: the
    introducer's roster, then any member it has read of but not yet
    numbered.

    ``rosters``: each member's roster as far as the introducer can tell it,
    one byte per entry. ``seen``: for each member's stream, the position
    through which the introducer has every message. ``introductions``:
    (introducer, position, member introduced). ``unsettled``: (sender,
    position, levels) for the acknowledgements the introducer could not yet
    apply.
    """

    rosters: list[bytes]
    seen: list[int]
    introductions: list[tuple[int, int, int]]
    unsettled: list[tuple[int, int, dict[int, int]]]


def hand_over(group: Group, order: Sequence[Member]) -> Handover:
    """Everything a new member needs to follow the group's rosters from
    where the introducer stands. ``order`` is the introducer's own roster,
    followed by any member it has read of but not yet numbered.

    >>> alice, bob, carol = b"a", b"b", b"c"
    >>> group = Group(
    ...     bases={alice: (alice, bob, carol), bob: (alice, bob)},
    ...     seen={alice: 4, bob: 2, carol: -1},
    ...     introductions={(alice, 1): carol},
    ...     acks={(bob, 2): {0: 6}},
    ... )
    >>> handed = hand_over(group, [alice, bob, carol])
    >>> handed.rosters
    [b'\\x00\\x01\\x02', b'\\x00\\x01', b'']
    >>> handed.seen, handed.introductions, handed.unsettled
    ([4, 2, -1], [(0, 1, 2)], [(1, 2, {0: 6})])
    """
    place = {member: index for index, member in enumerate(order)}
    rosters: list[bytes] = []
    unsettled: list[tuple[int, int, dict[int, int]]] = []
    for index, member in enumerate(order):
        followed = follow(group, member)
        entries = bytearray()
        for entry in followed.roster if followed else ():
            if entry not in place:
                break
            entries.append(place[entry])
        rosters.append(bytes(entries))
        for position, levels in followed.unsettled if followed else ():
            unsettled.append((index, position, dict(levels)))
    introductions = sorted(
        (place[introducer], position, place[introduced])
        for (introducer, position), introduced in group.introductions.items()
        if introducer in place and introduced in place
    )
    seen = [group.seen.get(member, -1) for member in order]
    return Handover(rosters, seen, introductions, unsettled)


def adopt(order: Sequence[Member], handed: Handover) -> Group:
    """A new member's first picture of the group, from what its introducer
    handed over. ``order`` is the list of members the reply named, in the
    order it named them. Anything that names a place ``order`` does not hold is
    dropped; a roster is kept only as far as its entries can be read. A
    member whose roster the introducer could not tell is left to be worked
    out from its own ``Introduction``.

    >>> alice, bob, carol = b"a", b"b", b"c"
    >>> group = adopt(
    ...     [alice, bob, carol],
    ...     Handover(
    ...         rosters=[b"\\x00\\x01\\x02", b"\\x00\\x01", b""],
    ...         seen=[4, 2, -1],
    ...         introductions=[(0, 1, 2)],
    ...         unsettled=[(1, 2, {0: 6})],
    ...     ),
    ... )
    >>> group.bases[alice], group.bases[bob], group.bases[carol]
    ((b'a', b'b', b'c'), (b'a', b'b'), Inherited(introducer=b'a', position=1))
    >>> group.seen, group.introductions, group.acks
    ({b'a': 4, b'b': 2, b'c': -1}, {(b'a', 1): b'c'}, {(b'b', 2): {0: 6}})
    """
    size = len(order)
    group = Group()
    for member, entries, seen in zip(order, handed.rosters, handed.seen):
        group.seen[member] = seen
        roster: list[Member] = []
        for index in entries:
            if index >= size:
                break
            roster.append(order[index])
        if roster:
            group.bases[member] = tuple(roster)
    for introducer, position, introduced in handed.introductions:
        if introducer < size and introduced < size:
            group.introductions[order[introducer], position] = order[
                introduced
            ]
            group.bases.setdefault(
                order[introduced], Inherited(order[introducer], position)
            )
    for sender, position, levels in handed.unsettled:
        if sender < size:
            group.acks[order[sender], position] = dict(levels)
    return group


def retire(group: Group, member: Member, token: Member) -> None:
    """Forget who ``member`` was while keeping every place it held: its key
    is replaced by ``token`` wherever it appears, so that every roster
    still counts the same.

    >>> alice, bob = b"a", b"b"
    >>> group = Group(
    ...     bases={alice: (alice, bob), bob: Inherited(alice, 0)},
    ...     seen={alice: 0, bob: 2},
    ...     introductions={(alice, 0): bob},
    ...     acks={(bob, 2): {0: 0}},
    ... )
    >>> retire(group, bob, placeholder(1))
    >>> group.bases[alice], group.introductions
    ((b'a', b'retired\\x01'), {(b'a', 0): b'retired\\x01'})
    >>> sorted(group.seen), list(group.acks)
    ([b'a', b'retired\\x01'], [(b'retired\\x01', 2)])
    """

    def renamed(who: Member) -> Member:
        return token if who == member else who

    bases: dict[Member, Roster | Inherited] = {}
    for owner, base in group.bases.items():
        if isinstance(base, Inherited):
            bases[renamed(owner)] = Inherited(
                renamed(base.introducer), base.position
            )
        else:
            bases[renamed(owner)] = tuple(renamed(entry) for entry in base)
    group.bases = bases
    group.seen = {renamed(who): seen for who, seen in group.seen.items()}
    group.introductions = {
        (renamed(introducer), position): renamed(introduced)
        for (introducer, position), introduced in group.introductions.items()
    }
    group.acks = {
        (renamed(sender), position): levels
        for (sender, position), levels in group.acks.items()
    }


def roster_of(group: Group, member: Member) -> Roster | None:
    """``member``'s roster as far as this client can tell it.

    >>> roster_of(Group(bases={b"a": (b"a", b"b")}), b"a")
    (b'a', b'b')
    """
    followed = follow(group, member)
    return None if followed is None else followed.roster


__all__ = [
    "ROSTER_MAX",
    "Followed",
    "Group",
    "Handover",
    "Inherited",
    "Levels",
    "Member",
    "Roster",
    "adopt",
    "follow",
    "grow",
    "hand_over",
    "placeholder",
    "resolve",
    "retire",
    "roster_of",
]
