"""Random runs of the group chat simulator, checking the spec's claims after
every step. Only what the spec promises loses nothing is run here: no
removal, no retention policy beyond dropping acked plaintext, no jumps."""

from __future__ import annotations

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

from katzenqt.groupchat import sim
from katzenqt.groupchat.member import CTX, Member

FOUNDERS = ("alice", "bob", "carol")
NEWCOMERS = ("dave", "erin")
NAMES = FOUNDERS + NEWCOMERS


class GroupChat(RuleBasedStateMachine):
    @initialize()
    def found(self) -> None:
        self.world = sim.World()
        self.world.run("new-group " + " ".join(FOUNDERS))
        self.joined_at = dict.fromkeys(FOUNDERS, 0)
        self.inviting: dict[str, tuple[str, dict[bytes, int]]] = {}
        """Who each introducer is inviting, and where it stood on each
        stream when the invitation began."""
        self.caught_up: dict[tuple[str, str], int] = {}

    @property
    def epoch(self) -> int:
        return self.world.replica.epoch

    def joined(self) -> list[str]:
        return [name for name in NAMES if name in self.world.joined]

    def member(self, name: str) -> Member:
        return self.world.members[name]

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
        me = self.member(introducer)
        stood = {
            key: cursor.next.index.idx_64 for key, cursor in me.cursors.items()
        }
        new = next(name for name in NEWCOMERS if name not in self.world.joined)
        if invite and introducer not in self.inviting:
            self.world.run(f"invite {introducer} {new}")
            self.inviting[introducer] = (new, stood)
            return
        new, stood = self.inviting.pop(introducer, (new, stood))
        if new in self.world.joined:
            return
        self.world.run(f"introduce {introducer} {new}")
        self.joined_at[new] = self.epoch
        joiner = self.member(new)
        for key, position in stood.items():
            if key in joiner.cursors:
                assert joiner.cursors[key].next.index.idx_64 <= position, (
                    f"{new} starts {self.world.name_of(key)}'s stream after"
                    f" where {introducer} stood when inviting it"
                )

    @rule(data=st.data())
    def catch_up(self, data: st.DataObject) -> None:
        """``reader`` reads every stream it follows. One it read to the end
        within the Sent-box window must be read to the end again. A stream
        handed over at a join promises nothing until it has been read to the
        end once: see stale_introducer.txt."""
        reader = data.draw(st.sampled_from(self.joined()))
        me = self.member(reader)
        for owner in self.joined():
            if owner == reader or self.member(owner).key not in me.cursors:
                continue
            self.world.run(f"read {reader} {owner}")
            end = self.member(owner).next.index.idx_64
            at_end = me.cursors[self.member(owner).key].next.index.idx_64 == end
            since = self.caught_up.get((reader, owner))
            if since is None and self.joined_at[owner] >= self.joined_at[reader]:
                since = self.joined_at[owner]
            if at_end:
                self.caught_up[reader, owner] = self.epoch
            elif since is not None:
                assert self.epoch - since > me.settings.window, (
                    f"{reader} is stuck on {owner}'s stream although it read"
                    f" to the end in epoch {since}, now {self.epoch}"
                )

    @invariant()
    def rosters_agree(self) -> None:
        """Everyone's view of a roster is a prefix of the roster itself."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            truth = self.member(owner).roster(self.member(owner).key)
            assert truth is not None
            for viewer in self.joined():
                view = self.member(viewer).roster(self.member(owner).key)
                if view is None:
                    continue
                assert truth.roster[: len(view.roster)] == view.roster, (
                    f"{viewer}'s view of {owner}'s roster is wrong"
                )

    @invariant()
    def acks_name_their_streams(self) -> None:
        """Every ack a member resolves is a box of the stream it names."""
        if not hasattr(self, "world"):
            return
        for viewer in self.joined():
            me = self.member(viewer)
            for owner, position in me.ack_values:
                for member, index in me.resolve(owner, position).items():
                    target = self.world.by_key(member)
                    target.read_cap.contains(index)

    @invariant()
    def acks_are_true(self) -> None:
        """An owner never counts a member as having read further than it
        has."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            me = self.member(owner)
            for acker, position in me.acked_by.items():
                cursor = self.world.by_key(acker).cursors[me.key]
                assert cursor.last is not None
                assert position <= cursor.last.idx_64

    @invariant()
    def nothing_owed_is_dropped(self) -> None:
        """A box a member has yet to read is not tombstoned because its
        plaintext was acked by everyone else."""
        if not hasattr(self, "world"):
            return
        for owner in self.joined():
            writer = self.member(owner)
            end = writer.next.index.idx_64
            for reader in self.joined():
                cursor = self.member(reader).cursors.get(writer.key)
                if cursor is None:
                    continue
                at = cursor.next
                while at.index.idx_64 < end:
                    stored = self.world.replica.read(at.box_id(CTX))
                    assert stored is None or stored.payload, (
                        f"{owner} tombstoned a box {reader} has not read"
                    )
                    at = at.next()


TestGroupChat = GroupChat.TestCase
TestGroupChat.settings = settings(
    max_examples=25, stateful_step_count=40, deadline=None
)


class GroupChatAtLength(GroupChat):
    pass


TestGroupChatAtLength = pytest.mark.nightly(GroupChatAtLength.TestCase)
TestGroupChatAtLength.settings = settings(
    max_examples=500, stateful_step_count=60, deadline=None
)
