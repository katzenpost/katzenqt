"""Rosters with explicit numbering: every roster index is announced, so a
missed message leaves an index unresolved and never shifts the others. See
"Rosters" in the group chat spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .wire import Key

At = tuple[Key, int]
"""An ``Introduction``: its introducer and its position on that stream."""


@dataclass
class Entry:
    key: Key | None = None
    introduced: At | None = None
    at: int = -1
    """Where the owner numbered it on its stream; -1 when the entry was in
    the roster it started with."""


@dataclass
class Rosters:
    """Every roster this member knows. A roster that began as a copy of an
    introducer's is not copied: its early entries are read from the
    introducer's roster as it stood at the ``Introduction``."""

    entries: dict[Key, dict[int, Entry]] = field(default_factory=dict)
    started: dict[Key, At] = field(default_factory=dict)
    introductions: dict[At, Key] = field(default_factory=dict)

    def found(self, founders: list[Key]) -> None:
        roster = {i: Entry(key) for i, key in enumerate(sorted(founders))}
        for founder in founders:
            self.entries[founder] = dict(roster)

    def resolve(self, owner: Key, index: int, at: int | None = None) -> Key | None:
        """Who ``owner`` numbered ``index``, counting only what it had
        numbered by position ``at``.

        >>> rosters = Rosters()
        >>> rosters.found([b"a", b"b"])
        >>> rosters.number(b"a", 2, (b"b", 7), at=4)
        >>> rosters.resolve(b"a", 2) is None
        True
        >>> rosters.introductions[b"b", 7] = b"c"
        >>> rosters.resolve(b"a", 2), rosters.resolve(b"a", 2, at=3)
        (b'c', None)
        >>> rosters.started[b"c"] = (b"a", 4)
        >>> rosters.resolve(b"c", 2), rosters.resolve(b"c", 0)
        (b'c', b'a')
        """
        entry = self.entries.get(owner, {}).get(index)
        if entry is not None:
            if at is not None and entry.at > at:
                return None
            if entry.key is not None:
                return entry.key
            return self.introductions.get(entry.introduced) if entry.introduced else None
        start = self.started.get(owner)
        if start is None:
            return None
        return self.resolve(start[0], index, start[1])

    def known(self, owner: Key, at: int | None = None) -> dict[int, Key | None]:
        """Every index ``owner`` is known to have assigned, and to whom."""
        return {index: key for index, (key, _) in self.numbered(owner, at).items()}

    def numbered(
        self, owner: Key, at: int | None = None
    ) -> dict[int, tuple[Key | None, int]]:
        """Every index ``owner`` is known to have assigned by position
        ``at``: to whom, and where. -1 for the roster it started with."""
        result: dict[int, tuple[Key | None, int]] = {}
        start = self.started.get(owner)
        if start is not None:
            result = {
                index: (key, -1)
                for index, (key, _) in self.numbered(start[0], start[1]).items()
            }
        for index, entry in self.entries.get(owner, {}).items():
            if at is None or entry.at <= at:
                result[index] = (self.resolve(owner, index, at), entry.at)
        return dict(sorted(result.items()))

    def size(self, owner: Key) -> int:
        known = self.known(owner)
        return max(known) + 1 if known else 0

    def index_of(self, owner: Key, member: Key) -> int | None:
        for index, key in self.known(owner).items():
            if key == member:
                return index
        return None

    def number(self, owner: Key, index: int, introduced: At | None, at: int) -> None:
        self.entries.setdefault(owner, {}).setdefault(
            index, Entry(introduced=introduced, at=at)
        )

    def retire(self, member: Key, token: Key) -> None:
        """Forget ``member``'s key, keeping every place it holds."""

        def renamed(key: Key) -> Key:
            return token if key == member else key

        for roster in self.entries.values():
            for entry in roster.values():
                if entry.key == member:
                    entry.key = token
                if entry.introduced is not None:
                    entry.introduced = (renamed(entry.introduced[0]), entry.introduced[1])
        if member in self.entries:
            self.entries[token] = self.entries.pop(member)
        self.started = {
            renamed(owner): (renamed(intro[0]), intro[1])
            for owner, intro in self.started.items()
        }
        self.introductions = {
            (renamed(intro[0]), intro[1]): renamed(key)
            for intro, key in self.introductions.items()
        }
