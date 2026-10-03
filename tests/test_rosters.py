"""Rosters: the rules one at a time, then a whole group run at random.

The random run is the real test. Members introduce, read, acknowledge and
send in any order, and messages can be delivered late, as one sent through a
substream is. At every step, whatever any client believes about any roster
must be true as far as it goes; once everyone has read everything, every
client must hold every roster exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import given, note, settings
from hypothesis import strategies as st

from katzenqt import rosters
from katzenqt.rosters import Group, Inherited, Member


def _key(number: int) -> Member:
    return bytes([number]) * 32


A, B, C, D, E = (_key(n) for n in range(1, 6))


def test_a_member_is_numbered_when_the_acknowledgement_reaches_it() -> None:
    known = {(B, 4): C}
    assert rosters.grow([A, B], {1: 3}, known) == []
    assert rosters.grow([A, B], {1: 4}, known) == [C]
    assert rosters.grow([A, B], {1: 57}, known) == [C]


def test_several_members_take_the_order_of_their_introducers() -> None:
    known = {(B, 2): D, (A, 9): C, (B, 1): E}
    assert rosters.grow([A, B], {0: 9, 1: 5}, known) == [C, E, D]


def test_an_acknowledgement_is_read_against_the_roster_before_it() -> None:
    known = {(B, 1): C, (C, 0): D}
    assert rosters.grow([A, B], {1: 1, 2: 0}, known) == [C]


def test_a_roster_is_told_only_as_far_as_the_watcher_has_read() -> None:
    group = Group(
        bases={A: (A, B)},
        seen={A: 5, B: 3},
        acks={(A, 2): {1: 7}, (A, 5): {1: 8}},
    )
    behind = rosters.follow(group, A)
    assert behind == rosters.Followed((A, B), ((2, {1: 7}), (5, {1: 8})))

    group.introductions[B, 6] = C
    group.seen[B] = 8
    assert rosters.follow(group, A) == rosters.Followed((A, B, C), ())


def test_a_late_message_takes_its_place_in_stream_order() -> None:
    group = Group(
        bases={A: (A, B, C)},
        seen={A: 0, B: 9, C: 9},
        introductions={(B, 1): D, (C, 1): E},
        acks={(A, 2): {2: 5}},
    )
    assert rosters.roster_of(group, A) == (A, B, C)

    group.acks[A, 1] = {1: 5}
    group.seen[A] = 2
    assert rosters.roster_of(group, A) == (A, B, C, D, E)


def test_a_new_member_starts_from_its_introducers_roster() -> None:
    group = Group(
        bases={A: (A,), B: Inherited(A, 0), C: Inherited(A, 3)},
        seen={A: 3, B: 0},
        introductions={(A, 0): B, (A, 3): C},
    )
    assert rosters.roster_of(group, B) == (A, B)
    assert rosters.roster_of(group, C) == (A, B, C)


def test_a_new_member_waits_for_an_introducer_not_yet_followed() -> None:
    group = Group(
        bases={A: (A, B), C: Inherited(A, 4)},
        seen={A: 4, B: 0},
        introductions={(A, 4): C, (B, 2): D},
        acks={(A, 3): {1: 2}},
    )
    assert rosters.roster_of(group, C) is None
    group.seen[B] = 2
    assert rosters.roster_of(group, C) == (A, B, D, C)


def test_resolving_skips_what_cannot_be_placed() -> None:
    group = Group(bases={A: (A, B)}, seen={A: 3})
    assert rosters.resolve(group, A, {1: 7, 5: 2}) == {B: 7}
    assert rosters.resolve(group, C, {0: 1}) == {}


def test_what_is_handed_over_lets_a_new_member_carry_on() -> None:
    introducer = Group(
        bases={A: (A, B, C), B: (A, B), C: Inherited(A, 1)},
        seen={A: 4, B: 2, C: 0},
        introductions={(A, 1): C},
        acks={(B, 2): {0: 4}, (B, 1): {0: 0}},
    )
    order = [A, B, C]
    handed = rosters.hand_over(introducer, order)
    newcomer = rosters.adopt(order, handed)
    for member in order:
        assert rosters.follow(newcomer, member) == rosters.follow(
            introducer, member
        )


def test_an_unsettled_acknowledgement_is_handed_over() -> None:
    introducer = Group(
        bases={A: (A, B), B: (A, B)},
        seen={A: 2, B: 6},
        acks={(B, 6): {0: 5}},
    )
    handed = rosters.hand_over(introducer, [A, B])
    assert handed.unsettled == [(1, 6, {0: 5})]

    newcomer = rosters.adopt([A, B], handed)
    newcomer.introductions[A, 4] = C
    newcomer.seen[A] = 5
    assert rosters.roster_of(newcomer, B) == (A, B, C)


def test_a_member_read_of_but_not_yet_numbered_is_handed_over() -> None:
    """The introducer has read C's Introduction of D but has not numbered
    D. The new member must still learn where D was introduced, or it would
    miscount every roster that numbers D later."""
    introducer = Group(
        bases={A: (A, B, C), B: (A, B, C), C: (A, B, C), D: Inherited(C, 0)},
        seen={A: 5, B: 2, C: 0, D: -1},
        introductions={(C, 0): D},
    )
    known = [A, B, C, D]
    newcomer = rosters.adopt(known, rosters.hand_over(introducer, known))
    assert newcomer.introductions == {(C, 0): D}
    assert rosters.roster_of(newcomer, D) == (A, B, C, D)

    newcomer.acks[B, 3] = {2: 0}
    newcomer.seen[B] = 3
    assert rosters.roster_of(newcomer, B) == (A, B, C, D)


def test_a_reply_that_names_a_place_it_does_not_hold_is_cut_short() -> None:
    handed = rosters.Handover(
        rosters=[bytes([0, 1, 7, 1]), bytes([9])],
        seen=[0, 0],
        introductions=[(0, 3, 9), (5, 1, 0)],
        unsettled=[(4, 1, {0: 1})],
    )
    group = rosters.adopt([A, B], handed)
    assert group.bases == {A: (A, B)}
    assert group.introductions == {}
    assert group.acks == {}


def test_a_forgotten_member_keeps_its_place() -> None:
    group = Group(
        bases={A: (A, B, C), C: Inherited(B, 2)},
        seen={A: 0, B: 2},
        introductions={(B, 2): C},
        acks={(A, 0): {1: 2}},
    )
    token = rosters.placeholder(1)
    rosters.retire(group, B, token)
    assert rosters.roster_of(group, A) == (A, token, C)
    assert group.bases[C] == Inherited(token, 2)
    assert group.introductions == {(token, 2): C}
    assert rosters.resolve(group, A, {1: 2, 2: 0}) == {token: 2, C: 0}


@dataclass
class _Message:
    levels: dict[int, int]
    introduces: Member | None = None


@dataclass
class _Client:
    """One member: its true roster, and what it believes of the others."""

    key: Member
    roster: list[Member]
    group: Group
    stream: list[_Message] = field(default_factory=list)
    next_read: dict[Member, int] = field(default_factory=dict)
    first_read: dict[Member, int] = field(default_factory=dict)
    held: dict[Member, set[int]] = field(default_factory=dict)
    advertised: dict[Member, int] = field(default_factory=dict)

    def delivered_through(self, stream: Member) -> int:
        """The position through which every message of ``stream`` has
        been delivered here."""
        held = self.held.get(stream)
        return (min(held) if held else self.next_read[stream]) - 1


class _World:
    def __init__(self) -> None:
        founder = _key(1)
        self.clients: dict[Member, _Client] = {
            founder: _Client(
                key=founder,
                roster=[founder],
                group=Group(bases={founder: (founder,)}),
            ),
        }

    def _pending_levels(self, client: _Client) -> dict[int, int]:
        levels: dict[int, int] = {}
        for stream in client.next_read:
            reached = client.delivered_through(stream)
            if stream not in client.roster:
                continue
            if reached < client.first_read[stream]:
                continue
            if reached > client.advertised.get(stream, -1):
                levels[client.roster.index(stream)] = reached
                client.advertised[stream] = reached
        return levels

    def _write(self, client: _Client, introduces: Member | None) -> int:
        # An Introduction carries no acknowledgements: the reply that goes
        # with it has already told the new member its place in the roster.
        levels = {} if introduces else self._pending_levels(client)
        numbered = rosters.grow(
            client.roster, levels, client.group.introductions
        )
        position = len(client.stream)
        client.stream.append(_Message(levels, introduces))
        client.roster.extend(numbered)
        client.group.seen[client.key] = position
        client.group.bases[client.key] = tuple(client.roster)
        return position

    def send(self, who: Member) -> None:
        self._write(self.clients[who], None)

    def introduce(self, who: Member, from_the_start: bool) -> None:
        introducer = self.clients[who]
        new = _key(len(self.clients) + 1)
        seen_before = dict(introducer.group.seen)
        position = self._write(introducer, new)
        order = list(introducer.roster)
        introducer.roster.append(new)
        introducer.group.bases[who] = tuple(introducer.roster)
        introducer.group.introductions[who, position] = new

        before = Group(
            bases={**introducer.group.bases, who: tuple(order)},
            seen={**introducer.group.seen, who: seen_before.get(who, -1)},
            introductions={
                at: member
                for at, member in introducer.group.introductions.items()
                if at != (who, position)
            },
            acks=introducer.group.acks,
        )
        # Members the introducer has read of but not yet numbered are
        # handed over too, after its roster.
        known = [*order, *sorted(set(introducer.next_read) - set(order))]
        handed = rosters.hand_over(before, known)
        group = rosters.adopt(known, handed)
        group.bases[who] = tuple(order) + (new,)
        group.bases[new] = tuple(order) + (new,)
        group.seen[new] = -1
        newcomer = _Client(key=new, roster=[*order, new], group=group)
        for member in known:
            if member == new:
                continue
            start = 0 if from_the_start else group.seen[member] + 1
            newcomer.next_read[member] = start
            newcomer.first_read[member] = start
        self.clients[new] = newcomer

        introducer.group.bases[new] = tuple(introducer.roster)
        introducer.group.seen[new] = -1
        introducer.next_read[new] = 0
        introducer.first_read[new] = 0

    def read(self, who: Member, stream: Member, late: bool) -> None:
        client = self.clients[who]
        position = client.next_read[stream]
        if position >= len(self.clients[stream].stream):
            return
        client.next_read[stream] = position + 1
        if late:
            client.held.setdefault(stream, set()).add(position)
        else:
            self._deliver(client, stream, position)

    def release(self, who: Member, stream: Member, position: int) -> None:
        client = self.clients[who]
        client.held[stream].discard(position)
        self._deliver(client, stream, position)

    def _deliver(
        self, client: _Client, stream: Member, position: int
    ) -> None:
        message = self.clients[stream].stream[position]
        if message.levels:
            client.group.acks[stream, position] = message.levels
        new = message.introduces
        if new is not None:
            client.group.introductions[stream, position] = new
            if new != client.key and new not in client.next_read:
                client.next_read[new] = 0
                client.first_read[new] = 0
                client.group.seen.setdefault(new, -1)
                client.group.bases.setdefault(
                    new, Inherited(stream, position)
                )
        client.group.seen[stream] = max(
            client.group.seen.get(stream, -1),
            client.delivered_through(stream),
        )

    def drain(self) -> None:
        """Everyone reads everything there is and takes delivery of it."""
        moved = True
        while moved:
            moved = False
            for client in self.clients.values():
                for stream in list(client.next_read):
                    for position in sorted(client.held.get(stream, ())):
                        self.release(client.key, stream, position)
                        moved = True
                    while client.next_read[stream] < len(
                        self.clients[stream].stream
                    ):
                        self.read(client.key, stream, late=False)
                        moved = True

    def check_nothing_believed_is_false(self) -> None:
        for client in self.clients.values():
            for member in client.group.bases:
                believed = rosters.roster_of(client.group, member)
                if believed is None:
                    continue
                truth = tuple(self.clients[member].roster)
                assert believed == truth[: len(believed)], (
                    f"{client.key[:1].hex()} believes "
                    f"{member[:1].hex()}'s roster is "
                    f"{[m[:1].hex() for m in believed]}, but it is "
                    f"{[m[:1].hex() for m in truth]}"
                )

    def check_everything_is_known(self) -> None:
        everyone = set(self.clients)
        for client in self.clients.values():
            assert set(client.next_read) | {client.key} == everyone
            for member in everyone:
                followed = rosters.follow(client.group, member)
                assert followed is not None
                assert followed.unsettled == ()
                assert followed.roster == tuple(self.clients[member].roster)


def _run(world: _World, data: st.DataObject, steps: int) -> None:
    for _ in range(steps):
        who = data.draw(st.sampled_from(sorted(world.clients)), label="who")
        client = world.clients[who]
        held = [
            (stream, position)
            for stream, positions in sorted(client.held.items())
            for position in sorted(positions)
        ]
        choices = ["send"]
        if len(world.clients) < 7:
            choices.append("introduce")
        if client.next_read:
            choices.append("read")
        if held:
            choices.append("release")
        action = data.draw(st.sampled_from(choices), label="action")
        if action == "send":
            world.send(who)
        elif action == "introduce":
            world.introduce(who, data.draw(st.booleans(), label="from start"))
        elif action == "read":
            stream = data.draw(
                st.sampled_from(sorted(client.next_read)), label="stream"
            )
            world.read(who, stream, data.draw(st.booleans(), label="late"))
        else:
            stream, position = data.draw(st.sampled_from(held), label="held")
            world.release(who, stream, position)
        note(f"{who[:1].hex()} {action}")
        world.check_nothing_believed_is_false()


@settings(max_examples=400, deadline=None)
@given(data=st.data(), steps=st.integers(min_value=1, max_value=60))
def test_a_group_run_at_random(data: st.DataObject, steps: int) -> None:
    world = _World()
    _run(world, data, steps)
    world.drain()
    world.check_nothing_believed_is_false()
    world.check_everything_is_known()


@settings(max_examples=200, deadline=None)
@given(data=st.data(), steps=st.integers(min_value=1, max_value=60))
def test_everyone_ends_up_numbering_everyone(
    data: st.DataObject, steps: int
) -> None:
    world = _World()
    _run(world, data, steps)
    # A member can be acknowledged only from the message after the one that
    # numbers it, so a chain of introductions takes one round per link.
    for _ in range(len(world.clients)):
        world.drain()
        for who in sorted(world.clients):
            world.send(who)
    world.drain()
    world.check_everything_is_known()
    everyone = set(world.clients)
    for client in world.clients.values():
        assert set(client.roster) == everyone


def test_a_roster_is_handed_over_only_as_far_as_its_members_are_listed() -> (
    None
):
    introducer = Group(bases={A: (A, B, C)}, seen={A: 0})
    handed = rosters.hand_over(introducer, [A, B])
    assert handed.rosters == [bytes([0, 1]), b""]
