"""Display ordering and membership-epoch metadata.

Arrival order remains the default. KQT_ORDERING selects an optional display
order without changing stored order or read tracking; epoch colors mark local
membership changes. Outgoing messages are unchanged.
"""

from __future__ import annotations

import colorsys
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import GroupChatMessage

__all__ = [
    "MessageMeta",
    "OrderingStrategy",
    "InsertionOrder",
    "AuthorLaneOrder",
    "EpochAnchoredOrder",
    "get_strategy",
    "active_strategy",
    "STRATEGIES",
    "OutboundAnnotator",
    "NoAnnotation",
    "active_annotator",
    "GraphNode",
    "GraphLane",
    "MessageGraph",
    "build_message_graph",
    "EpochRow",
    "annotate_epochs",
    "epoch_color",
    "MEMBERSHIP_SENTINELS",
    "MEMBERSHIP_HASH_SENTINEL",
    "normalize_membership_hash",
]

MEMBERSHIP_SENTINELS = (b"TODO" * 8, bytes(32))
MEMBERSHIP_HASH_SENTINEL = MEMBERSHIP_SENTINELS[0]

_ORDERING_ENV = "KQT_ORDERING"


def normalize_membership_hash(value: "bytes | None") -> "bytes | None":
    """Map an absent or sentinel membership hash to ``None`` ("unknown"), so no
    consumer colours a fake epoch or reports a mismatch for a placeholder."""
    if value is None or value in MEMBERSHIP_SENTINELS:
        return None
    return value


@dataclass(frozen=True)
class MessageMeta:
    """The minimum a row contributes to ordering, colouring, and the graph.

    ``peer_id`` is the LANE identity: two distinct identities can legitimately
    share a display name, so lanes key on the id and use ``author`` only as a
    label.

    The two epochs are deliberately separate -- conflating them leaks reading
    progress into the colouring:

    * ``arrival_epoch`` -- the RECEIVER-local membership hash, reconstructed by
      replaying INTRODUCTION/LEAVE locally (never from the wire). Drives the
      per-message colour and the striped epoch dividers. This is "the state the
      message arrived in".
    * ``sender_epoch`` -- the membership hash the SENDER stamped on the wire
      (``GroupChatMessage.membership_hash``). Drives the "membership matches"
      banner and the epoch-anchored ordering. Carries no reading-progress, but
      it is hostile-peer input, so it is only ever compared, never trusted as a
      lane identity.
    """

    conversation_order: int
    peer_id: int
    author: str
    message_id: str
    arrival_epoch: "bytes | None"
    sender_epoch: "bytes | None" = None


@runtime_checkable
class OrderingStrategy(Protocol):
    name: str

    def order(self, metas: Sequence[MessageMeta]) -> list[int]:
        """Return ``conversation_order`` values in display order."""
        ...


class InsertionOrder:
    """Default: ascending ``conversation_order`` -- identical to today."""

    name = "insertion"

    def order(self, metas: Sequence[MessageMeta]) -> list[int]:
        return [m.conversation_order for m in
                sorted(metas, key=lambda m: m.conversation_order)]


class AuthorLaneOrder:
    """Experiment/demo: group every author's rows together (lanes ordered by
    first appearance, rows within a lane kept in ``conversation_order``). This
    is the linear projection of the swimlane graph, not a convergent order."""

    name = "author"

    def order(self, metas: Sequence[MessageMeta]) -> list[int]:
        by_order = sorted(metas, key=lambda m: m.conversation_order)
        lanes: dict[int, list[int]] = {}
        first_seen: list[int] = []
        for m in by_order:
            if m.peer_id not in lanes:
                lanes[m.peer_id] = []
                first_seen.append(m.peer_id)
            lanes[m.peer_id].append(m.conversation_order)
        out: list[int] = []
        for peer_id in first_seen:
            out.extend(lanes[peer_id])
        return out


class EpochAnchoredOrder:
    """Option E: causal *layering* by membership epoch without a wire extension.

    Each message already carries the sender's membership hash
    (``sender_epoch``); this groups rows into epoch bands in the order the
    epochs first appear, keeping arrival order within a band. A late-arriving
    message composed under an older room state therefore stays anchored to that
    state rather than jumping to the bottom -- the leak is bounded to "which
    membership state" (derived from membership, not from what the sender had
    read), which a peer can already infer.

    Convergence honesty: with ``tiebreak="arrival"`` the within-band order is
    the receiver-local ``conversation_order``, so two clients need NOT render a
    band identically -- it is a local view. ``tiebreak="message_id"`` is
    cross-client identical IFF ``message_id`` is a cross-client-stable value
    (a content hash, not a local row id); the caller owns that guarantee.
    """

    name = "epoch"

    def __init__(self, tiebreak: str = "arrival") -> None:
        self.tiebreak = tiebreak

    def _within(self, m: MessageMeta) -> "tuple[object, ...]":
        if self.tiebreak == "message_id":
            return (m.message_id, m.conversation_order)
        return (m.conversation_order,)

    def order(self, metas: Sequence[MessageMeta]) -> list[int]:
        by_order = sorted(metas, key=lambda m: m.conversation_order)
        rank: dict["bytes | None", int] = {}
        for m in by_order:
            epoch = normalize_membership_hash(m.sender_epoch)
            if epoch not in rank:
                rank[epoch] = len(rank)
        ordered = sorted(
            by_order,
            key=lambda m: (
                rank[normalize_membership_hash(m.sender_epoch)],
                self._within(m),
            ),
        )
        return [m.conversation_order for m in ordered]


STRATEGIES: dict[str, OrderingStrategy] = {
    s.name: s for s in (InsertionOrder(), AuthorLaneOrder(), EpochAnchoredOrder())
}


def get_strategy(name: "str | None") -> OrderingStrategy:
    """The named strategy, or the insertion default for an unknown/empty name."""
    if name and name in STRATEGIES:
        return STRATEGIES[name]
    return STRATEGIES["insertion"]


def active_strategy() -> OrderingStrategy:
    """The strategy selected by ``KQT_ORDERING`` (insertion default)."""
    return get_strategy(os.environ.get(_ORDERING_ENV))




@runtime_checkable
class OutboundAnnotator(Protocol):
    name: str

    def annotate(
        self, gcm: "GroupChatMessage", metas: Sequence[MessageMeta]
    ) -> "GroupChatMessage":
        """Return the message to send, optionally carrying causal metadata."""
        ...


class NoAnnotation:
    """Default: attach nothing, change no bytes on the wire.

    A real annotator (e.g. one attaching a per-message seen-vector so a
    convergent causal order becomes computable) must NOT be enabled until:
    (a) ``GroupChatMessage.from_cbor`` is confirmed to ignore unknown CBOR keys;
    (b) every other client's INDEPENDENT decoder on the shared group is
    confirmed to tolerate them too; (c) the new field gets bounded validation as
    hostile-peer input and stays opt-in -- a seen-vector leaks how much of the
    conversation the sender has read.
    """

    name = "none"

    def annotate(
        self, gcm: "GroupChatMessage", metas: Sequence[MessageMeta]
    ) -> "GroupChatMessage":
        return gcm


def active_annotator() -> OutboundAnnotator:
    """The outbound annotator; only the no-op ships today."""
    return NoAnnotation()




@dataclass(frozen=True)
class EpochRow:
    """One row in the INLINE chat stream, coloured by the arrival epoch. This is
    the shape the clients render: a single ordered column (not a separate graph)
    where the background colour is the local membership state the message
    arrived in, and ``is_boundary`` marks where that state changed from the row
    above -- the striped divider. An unknown/sentinel arrival epoch inherits the
    last known state (every row keeps a provenance) rather than showing blank."""

    message_id: str
    conversation_order: int
    arrival_epoch: "bytes | None"
    color: str
    is_boundary: bool


def annotate_epochs(metas: Sequence[MessageMeta]) -> list[EpochRow]:
    """Annotate rows ALREADY in display order with arrival-epoch colour and the
    striped-divider boundary. A boundary fires only on a real change between two
    KNOWN epochs; an unknown epoch carries the previous known state forward (so
    it neither starts nor ends an epoch), matching the graph's lane logic."""
    rows: list[EpochRow] = []
    last_known: "bytes | None" = None
    for m in metas:
        h = normalize_membership_hash(m.arrival_epoch)
        boundary = h is not None and last_known is not None and h != last_known
        effective = h if h is not None else last_known
        if h is not None:
            last_known = h
        rows.append(EpochRow(
            message_id=m.message_id,
            conversation_order=m.conversation_order,
            arrival_epoch=effective,
            color=epoch_color(effective),
            is_boundary=boundary,
        ))
    return rows


@dataclass(frozen=True)
class GraphNode:
    message_id: str
    conversation_order: int
    membership_hash: "bytes | None"
    epoch: int


@dataclass(frozen=True)
class GraphLane:
    peer_id: int
    author: str
    nodes: tuple[GraphNode, ...]


@dataclass(frozen=True)
class MessageGraph:
    lanes: tuple[GraphLane, ...]


def build_message_graph(metas: Sequence[MessageMeta]) -> MessageGraph:
    """Group rows into one swimlane per author IDENTITY (``peer_id``), ordered
    within a lane by ``conversation_order``. A lane's epoch increments each time
    the membership hash changes from the previous non-``None`` hash in that lane,
    so a colour break marks exactly where that sender's membership view shifted.
    Lanes are ordered by first appearance for a stable layout."""
    by_order = sorted(metas, key=lambda m: m.conversation_order)
    order_of_lane: list[int] = []
    rows: dict[int, list[MessageMeta]] = {}
    label: dict[int, str] = {}
    for m in by_order:
        if m.peer_id not in rows:
            rows[m.peer_id] = []
            order_of_lane.append(m.peer_id)
            label[m.peer_id] = m.author
        rows[m.peer_id].append(m)

    lanes: list[GraphLane] = []
    for peer_id in order_of_lane:
        nodes: list[GraphNode] = []
        epoch = 0
        last_hash: "bytes | None" = None
        for m in rows[peer_id]:
            h = normalize_membership_hash(m.arrival_epoch)
            if h is not None and last_hash is not None and h != last_hash:
                epoch += 1
            if h is not None:
                last_hash = h
            nodes.append(GraphNode(
                message_id=m.message_id,
                conversation_order=m.conversation_order,
                membership_hash=h,
                epoch=epoch,
            ))
        lanes.append(GraphLane(
            peer_id=peer_id, author=label[peer_id], nodes=tuple(nodes)
        ))
    return MessageGraph(lanes=tuple(lanes))


def epoch_color(membership_hash: "bytes | None") -> str:
    """A deterministic ``#rrggbb`` for a membership hash: equal hashes get the
    SAME colour in every lane (so "the hash matches" reads as one colour and "it
    changed" as a new one). Hue derives from the hash bytes; saturation and
    lightness are fixed to stay legible in both light and dark themes. ``None``
    ("no provenance") is a neutral grey."""
    if membership_hash is None:
        return "#9e9e9e"
    hue = (int.from_bytes(membership_hash[:4], "big") % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.55)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
