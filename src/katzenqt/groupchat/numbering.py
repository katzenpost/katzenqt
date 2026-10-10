"""Rosters with explicit numbering: every roster index is announced, so a
missed message leaves an index unresolved and never shifts the others. See
"Rosters" in the group chat spec.

Indexes are assigned in order and never change, so a roster as it stood
when it assigned index ``k`` is its entries up to ``k``: no record of when
anything happened is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .wire import Key

At = tuple[Key, bytes]
"""An ``Introduction``: its introducer and its ``MessageBoxIndex``."""


@dataclass
class Entry:
    key: Key | None = None
    introduced: At | None = None


@dataclass
class Rosters:
    """Every roster this member knows. A roster that began as a copy of an
    introducer's is not copied: its early entries are read from the
    introducer's roster, up to the index it assigned the new member."""

    entries: dict[Key, dict[int, Entry]] = field(default_factory=dict)
    started: dict[Key, tuple[Key, int]] = field(default_factory=dict)
    """A member whose roster began as its introducer's, through the index
    the introducer assigned it."""
    introductions: dict[At, Key] = field(default_factory=dict)

    def found(self, founders: list[Key]) -> None:
        roster = {i: Entry(key) for i, key in enumerate(sorted(founders))}
        for founder in founders:
            self.entries[founder] = dict(roster)

    def resolve(self, owner: Key, index: int) -> Key | None:
        """Who ``owner`` numbered ``index``, if this member can tell.

        >>> rosters = Rosters()
        >>> rosters.found([b"a", b"b"])
        >>> rosters.number(b"a", 2, (b"b", b"i7"))
        >>> rosters.resolve(b"a", 2) is None
        True
        >>> rosters.introductions[b"b", b"i7"] = b"c"
        >>> rosters.resolve(b"a", 2)
        b'c'
        >>> rosters.started[b"c"] = (b"a", 2)
        >>> rosters.resolve(b"c", 2), rosters.resolve(b"c", 0), rosters.resolve(b"c", 3)
        (b'c', b'a', None)
        """
        return self._resolve(owner, index, frozenset())

    def _resolve(self, owner: Key, index: int, seen: frozenset[Key]) -> Key | None:
        entry = self.entries.get(owner, {}).get(index)
        if entry is not None:
            if entry.key is not None:
                return entry.key
            return self.introductions.get(entry.introduced) if entry.introduced else None
        start = self.started.get(owner)
        if start is None or index > start[1] or owner in seen:
            return None
        return self._resolve(start[0], index, seen | {owner})

    def known(self, owner: Key) -> dict[int, Key | None]:
        """Every index ``owner`` is known to have assigned, and to whom."""
        return {
            index: self.resolve(owner, index)
            for index in sorted(self._indexes(owner, frozenset()))
        }

    def _indexes(self, owner: Key, seen: frozenset[Key]) -> set[int]:
        indexes = set(self.entries.get(owner, {}))
        start = self.started.get(owner)
        if start is not None and owner not in seen:
            indexes |= {
                i for i in self._indexes(start[0], seen | {owner}) if i <= start[1]
            }
        return indexes

    def size(self, owner: Key) -> int:
        known = self.known(owner)
        return max(known) + 1 if known else 0

    def index_of(self, owner: Key, member: Key) -> int | None:
        for index, key in self.known(owner).items():
            if key == member:
                return index
        return None

    def number(self, owner: Key, index: int, introduced: At | None) -> None:
        self.entries.setdefault(owner, {}).setdefault(index, Entry(introduced=introduced))

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
            renamed(owner): (renamed(start[0]), start[1])
            for owner, start in self.started.items()
        }
        self.introductions = {
            (renamed(intro[0]), intro[1]): renamed(key)
            for intro, key in self.introductions.items()
        }
