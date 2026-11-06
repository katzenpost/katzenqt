"""Random runs of the group chat state machine against the simulated
Pigeonhole, checking the spec's claims after every step. Only what the spec
promises loses nothing is run here: no removal and no retention policy
beyond dropping acked plaintext."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import IO

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from katzenqt.groupchat import sim, state
from katzenqt.groupchat.group import Group
from katzenqt.groupchat.ledger import Induction
from katzenqt.groupchat.trace import TracedGroup
from katzenqt.groupchat.wire import Key

FOUNDERS = ("alice", "bob", "carol")
NEWCOMERS = ("dave", "erin")
NAMES = FOUNDERS + NEWCOMERS
TRACE_DIR = os.environ.get("GROUPCHAT_TRACE_DIR")
"""Where to write a trace of every run, for Cryptlib's replay."""


class GroupChat(RuleBasedStateMachine):
    passive = False
    """Drive members as a client that reads and writes for itself would,
    with its writes landing late."""

    trace: IO[str] | None = None

    @initialize()
    def found(self) -> None:
        if TRACE_DIR:
            name = f"{type(self).__name__}-{uuid.uuid4().hex[:12]}.jsonl"
            self.trace = (Path(TRACE_DIR) / name).open("w")
        self.world = sim.World(self.trace)
        if self.passive:
            self.world.run("option passive on")
            self.world.run("option delay-writes on")
        self.world.run("new-group " + " ".join(FOUNDERS))
        self.since: dict[str, int] = dict.fromkeys(FOUNDERS, 0)
        """When each member could first read from where it started."""
        self.inviting: dict[str, tuple[str, dict[Key, int]]] = {}
        """Who each introducer is inviting, and where it had read each
        stream when the invitation began."""
        self.caught_up: dict[tuple[str, str], int] = {}

    @property
    def epoch(self) -> int:
        return self.world.replica.epoch

    def joined(self) -> list[str]:
        return [name for name in NAMES if name in self.world.joined]

    def group(self, name: str) -> Group:
        return self.world.group(name)

    def key(self, name: str) -> Key:
        return self.world.members[name].key

    def n(self, owner: Key, index: bytes) -> int:
        """Which box of ``owner``'s stream ``index`` names: for checking,
        by stepping the stream forward."""
        found = self.world.ordinal(owner, index)
        assert found is not None
        return found

    def end(self, owner: str) -> int:
        """Which box ``owner`` writes next."""
        member = self.world.members[owner]
        nxt = member.next_own if member.passive else self.group(owner).pool[0]
        return self.n(member.key, nxt)

    @precondition(lambda self: self.passive)
    @rule(data=st.data())
    def land(self, data: st.DataObject) -> None:
        self.world.run(f"land {data.draw(st.sampled_from(self.joined()))}")

    @precondition(lambda self: self.passive)
    @rule(data=st.data())
    def abandon(self, data: st.DataObject) -> None:
        self.world.run(f"abandon {data.draw(st.sampled_from(self.joined()))}")

    def pair(self, data: st.DataObject) -> tuple[str, str]:
        reader = data.draw(st.sampled_from(self.joined()))
        owners = [name for name in self.joined() if name != reader]
        return reader, data.draw(st.sampled_from(owners))

    @rule(data=st.data())
    def send(self, data: st.DataObject) -> None:
        self.world.run(f"send {data.draw(st.sampled_from(self.joined()))} hi")

    @rule(data=st.data())
    def read(self, data: st.DataObject) -> None:
        self.world.run("read {} {}".format(*self.pair(data)))

    @rule(data=st.data())
    def scan(self, data: st.DataObject) -> None:
        self.world.run("scan {} {}".format(*self.pair(data)))

    @rule(data=st.data())
    def reply(self, data: st.DataObject) -> None:
        """One member reads everything, then sends."""
        who = data.draw(st.sampled_from(self.joined()))
        for owner in self.joined():
            if owner != who:
                self.world.run(f"read {who} {owner}")
        self.world.run(f"send {who} hi")

    @rule()
    def round(self) -> None:
        """Everyone reads everything, then everyone sends."""
        for reader in self.joined():
            for owner in self.joined():
                if owner != reader:
                    self.world.run(f"read {reader} {owner}")
        for who in self.joined():
            self.world.run(f"send {who} hi")

    @rule()
    def tick(self) -> None:
        self.world.run("tick")

    @precondition(lambda self: len(self.world.joined) < len(NAMES))
    @rule(data=st.data(), invite=st.booleans())
    def introduce(self, data: st.DataObject, invite: bool) -> None:
        introducer = data.draw(st.sampled_from(self.joined()))
        group = self.group(introducer)
        new = next(name for name in NEWCOMERS if name not in self.world.joined)
        if introducer not in self.inviting:
            if new in (name for name, _ in self.inviting.values()):
                return
            self.world.run(f"invite {introducer} {new}")
            assert group.ledger.induction is Induction.INDUCTING
            stood = {key: self.n(key, reader.next) for key, reader in group.readers.items()}
            self.inviting[introducer] = (new, stood)
            self.since[new] = self.epoch
            self.inherit(introducer, new)
            if invite:
                return
        new, starts = self.inviting.pop(introducer)
        self.caught_up[new, introducer] = self.epoch
        self.world.run(f"introduce {introducer} {new}")
        joiner = self.group(new)
        for key, start in starts.items():
            if key in joiner.readers:
                assert self.n(key, joiner.readers[key].next) <= start, (
                    f"{new} starts {self.world.name_of(key)}'s stream after"
                    f" where {introducer} had read it when inviting"
                )

    def inherit(self, introducer: str, new: str) -> None:
        """A new member starts where its introducer has read, so it can only
        be promised what its introducer could: each stream the introducer
        has read to the end, from when it did."""
        group = self.group(introducer)
        for owner in self.joined():
            stream = group.readers.get(self.key(owner))
            if stream is None:
                continue
            if self.n(stream.stream, stream.next) == self.end(owner):
                self.caught_up[new, owner] = self.epoch
            elif (introducer, owner) in self.caught_up:
                self.caught_up[new, owner] = self.caught_up[introducer, owner]

    @rule(data=st.data())
    def catch_up(self, data: st.DataObject) -> None:
        """``reader`` reads every stream it follows. One it could read to
        the end within the Sent-box window must be read to the end again."""
        reader = data.draw(st.sampled_from(self.joined()))
        group = self.group(reader)
        for owner in self.joined():
            stream = group.readers.get(self.key(owner))
            if stream is None:
                continue
            self.world.run(f"read {reader} {owner}")
            end = self.end(owner)
            since = self.caught_up.get((reader, owner))
            if since is None and self.since[owner] >= self.since[reader]:
                since = self.since[owner]
            if self.n(stream.stream, stream.next) == end:
                self.caught_up[reader, owner] = self.epoch
            elif since is not None:
                window = group.policy.window
                assert window is not None and self.epoch - since > window, (
                    f"{reader} is stuck on {owner}'s stream although it"
                    f" could read to the end in epoch {since}, now {self.epoch}"
                )

    @invariant()
    def survives_a_restart(self) -> None:
        """Every member's state comes back the same from its bytes, and the
        run carries on from the copy."""
        if not hasattr(self, "world"):
            return
        for name in self.joined():
            member = self.world.members[name]
            assert member.group is not None
            restored = state.load(state.dump(member.group))
            if isinstance(member.group, TracedGroup):
                restored = TracedGroup.of(restored, member.group.tracer)
            assert restored == member.group
            member.group = restored

    @invariant()
    def rosters_agree(self) -> None:
        """Whoever a member can name at an index of a roster is whoever the
        roster's owner put there."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            truth = self.group(owner).rosters.known(self.key(owner))
            for viewer in self.joined():
                view = self.group(viewer).rosters.known(self.key(owner))
                for index, key in view.items():
                    if key is not None:
                        assert truth.get(index) == key, (
                            f"{viewer} thinks {owner}'s index {index} is"
                            f" {self.world.name_of(key)}"
                        )

    @invariant()
    def nobody_numbered_twice(self) -> None:
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            keys = [
                key
                for key in self.group(owner).rosters.known(self.key(owner)).values()
                if key is not None
            ]
            assert len(keys) == len(set(keys)), f"{owner} numbered someone twice"

    @invariant()
    def acks_name_their_streams(self) -> None:
        """Every ack a member resolves is a box of the stream it names."""
        if not hasattr(self, "world"):
            return
        for viewer in self.joined():
            rosters = self.group(viewer).rosters
            for (sender, _), message in self.world.members[viewer].seen.items():
                for index, value in message.acks.items():
                    acked = rosters.resolve(sender, index)
                    if acked is not None:
                        assert self.world.ordinal(acked, value) is not None

    @invariant()
    def acks_are_true(self) -> None:
        """An owner never counts a member as having read further than it
        has."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            group = self.group(owner)
            records = group.sent.records
            for acker, idx64 in group.ledger.acked_by.items():
                if idx64 not in records:
                    continue
                reader = self.group(self.world.name_of(acker)).readers[self.key(owner)]
                assert reader.run is not None
                assert self.n(self.key(owner), records[idx64].index) <= self.n(self.key(owner), reader.run)

    @invariant()
    def acks_cover_only_boxes_read(self) -> None:
        """A member acknowledges a box only once it has read it and every
        box before it from where it started reading the stream."""
        if not hasattr(self, "world"):
            return
        for viewer in self.joined():
            member = self.world.members[viewer]
            for stream, value in self.group(viewer).ledger.published.items():
                tried = [self.n(stream, index) for index in member.tried.get(stream, ())]
                had = {
                    self.n(stream, index) for owner, index in member.had if owner == stream
                }
                missing = set(range(min(tried, default=0), self.n(stream, value) + 1)) - had
                assert not missing, (
                    f"{viewer} acked {self.world.name_of(stream)}'s box "
                    f"{self.n(stream, value)} without reading {sorted(missing)}"
                )

    @invariant()
    def nothing_owed_is_dropped(self) -> None:
        """A box a member has yet to read is never tombstoned: it is
        dropped only once everyone who reads it has acked it."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            writer = self.world.members[owner]
            end = self.end(owner)
            for reader in self.joined():
                stream = self.group(reader).readers.get(writer.key)
                if stream is None:
                    continue
                for at in range(self.n(writer.key, stream.next), end):
                    assert self.world.do_peek(owner, str(at)) != [
                        "tombstone"
                    ], f"{owner} tombstoned a box {reader} has not read"


    def teardown(self) -> None:
        if self.trace is not None:
            self.trace.close()


TestGroupChat = GroupChat.TestCase
TestGroupChat.settings = settings(
    max_examples=25, stateful_step_count=40, deadline=None
)


class PassiveGroupChat(GroupChat):
    passive = True


TestPassiveGroupChat = PassiveGroupChat.TestCase
TestPassiveGroupChat.settings = settings(
    max_examples=25, stateful_step_count=40, deadline=None
)


class GroupChatAtLength(GroupChat):
    pass


class PassiveGroupChatAtLength(GroupChat):
    passive = True


TestPassiveGroupChatAtLength = pytest.mark.nightly(PassiveGroupChatAtLength.TestCase)
TestPassiveGroupChatAtLength.settings = settings(
    max_examples=500, stateful_step_count=60, deadline=None
)


TestGroupChatAtLength = pytest.mark.nightly(GroupChatAtLength.TestCase)
TestGroupChatAtLength.settings = settings(
    max_examples=500, stateful_step_count=60, deadline=None
)
