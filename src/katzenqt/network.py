import secrets
import katzenpost_thinclient
from katzenpost_thinclient import (
    ThinClientOfflineError,
    BACAPDecryptionFailedError, StartResendingCancelledError,
    DatabaseFailureError, BoxIDNotFoundError, TombstoneError,
CourierError, CourierInvalidEpochError, ReplicaError,
)
from katzenpost_thinclient import Config as ThinClientConfig
import hashlib
import errno
import importlib.resources
import os
import struct
import threading
import time
import types
import nacl.public
import secrets
import random
import logging
# https://github.com/katzenpost/thin_client/blob/main/examples/echo_ping.py
import asyncio
import traceback
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable, Hashable, Iterable
from datetime import datetime, timezone
from typing import (
    Literal,
    NamedTuple,
    Protocol,
    TypedDict,
    TypeVar,
    Unpack,
)

import cbor2

from .katzen_util import create_task
from ._thinclient import ThinClient
from pydantic.dataclasses import dataclass
from . import attachment_images, conversation_handlers, models, persistent
from sqlmodel import select
from sqlalchemy.exc import IntegrityError, OperationalError

logger = logging.getLogger("katzen.network")


class MixnetStats:
    """Process-lifetime counters for pigeonhole read/write operations.

    Incremented on the io loop by the wrappers installed in
    ``install_stats_counters`` and read from the Qt loop by the Mixnet-status
    "Stats" window. Plain ints, so reads under the GIL are safe without a lock.
    """

    def __init__(self) -> None:
        # encrypt_read/encrypt_write calls (envelope preparation).
        self.reads_prepared = 0
        self.writes_prepared = 0
        # Actual pigeonhole sends (start_resending_encrypted_message), so a
        # read re-cast or a write resend counts each time.
        self.reads_sent = 0
        self.writes_sent = 0
        # Read outcomes.
        self.reads_with_payload = 0
        self.reads_boxnotfound = 0
        # Write outcome: the courier acknowledged the envelope.
        self.writes_acked = 0
        # Every send, plus its live in-flight gauge and its abandonment
        # outcomes. packets_sent == reads_sent + writes_sent.
        self.packets_sent = 0
        self.packets_in_flight = 0
        self.packets_timed_out = 0
        self.packets_link_down = 0


stats = MixnetStats()

# (attribute, display label) in display order, shared by the Stats window.
STATS_FIELDS = (
    ("reads_prepared", "Reads prepared (encrypt_read)"),
    ("reads_sent", "Reads sent"),
    ("reads_with_payload", "Reads with payload"),
    ("reads_boxnotfound", "Reads with BoxIDNotFound"),
    ("writes_prepared", "Writes prepared (encrypt_write)"),
    ("writes_sent", "Writes sent (incl. resends)"),
    ("writes_acked", "Writes acknowledged"),
    ("packets_sent", "Packets sent (total)"),
    ("packets_in_flight", "Packets in flight"),
    ("packets_timed_out", "Packets timed out"),
    ("packets_link_down", "Packets link-down"),
)

# Fields rendered with a percentage of another field, e.g. timed-out packets
# as a share of all packets sent.
STATS_PERCENTAGES = {
    "packets_timed_out": "packets_sent",
}


def reset_stats() -> None:
    """Zero every counter in place (test helper)."""
    for key, _ in STATS_FIELDS:
        setattr(stats, key, 0)


def stats_snapshot() -> "dict[str, int]":
    """A copy of the counters, safe to read from the Qt thread."""
    return {key: getattr(stats, key) for key, _ in STATS_FIELDS}


# ---------------------------------------------------------------------------
# Per-packet records (Packets window)
# ---------------------------------------------------------------------------

# How many finished packets the Packets window retains; the combo box offers
# these values and defaults to the first non-zero one.
PACKET_FINISHED_LIMIT_OPTIONS = (0, 5, 10, 100)
DEFAULT_PACKET_FINISHED_LIMIT = 5

# Terminal packet statuses.
PACKET_STATUS_IN_FLIGHT = "in_flight"
PACKET_STATUS_PAYLOAD = "payload"
PACKET_STATUS_EMPTY = "empty"
PACKET_STATUS_BOXNOTFOUND = "boxnotfound"
PACKET_STATUS_ACKED = "acked"
PACKET_STATUS_TIMED_OUT = "timed_out"
PACKET_STATUS_LINK_DOWN = "link_down"
PACKET_STATUS_CANCELLED = "cancelled"
PACKET_STATUS_ERROR = "error"


class PacketContext:
    """Per-send metadata the call site hands to the send wrapper.

    Passed to ``connection.start_resending_encrypted_message`` as the private
    ``_packet_context`` kwarg (the wrapper pops it before calling the real
    method) and to ``_rpc_racing_connection_life`` so a lost race can set
    ``timed_out`` before the wrapper records the terminal status.
    """

    __slots__ = (
        "kind", "stream_id", "box_index", "box_position", "timeout_s",
        "timed_out", "packet_id", "stage", "label",
    )

    def __init__(self, kind: str, *, stream_id=None, box_index=None,
                 box_position=None, timeout_s=None, stage=None, label=None):
        self.kind = kind
        self.stream_id = stream_id
        self.box_index = box_index
        self.box_position = box_position
        self.timeout_s = timeout_s
        self.stage = stage
        self.label = label
        self.timed_out = False
        self.packet_id = None


class _PacketRecord:
    __slots__ = (
        "id", "kind", "stream_id", "box_index", "box_position", "attempt",
        "sent_at", "sent_wall", "timeout_s", "status", "finished_at",
        "envelope_hash", "stage", "label",
    )

    def __init__(self, packet_id, context, envelope_hash):
        self.id = packet_id
        self.kind = context.kind
        self.stream_id = context.stream_id
        self.box_index = context.box_index
        self.box_position = context.box_position
        self.attempt = _next_packet_attempt(context)
        self.sent_at = time.monotonic()
        self.sent_wall = time.time()
        self.timeout_s = context.timeout_s
        self.status = PACKET_STATUS_IN_FLIGHT
        self.finished_at = None
        self.envelope_hash = envelope_hash
        self.stage = context.stage
        self.label = context.label


_packets: "dict[str, _PacketRecord]" = {}
# Finished packet ids in completion order (oldest first), pruned to the
# configured retention limit. In-flight packets are always kept.
_packet_finished_order: "list[str]" = []
_packet_finished_limit = DEFAULT_PACKET_FINISHED_LIMIT
# Send attempts per (stream, box) -- or (kind, box) for streamless vouchers --
# so the Packets window can show how many times a box has been retried. Capped
# FIFO, since a long session touches many distinct boxes.
_packet_attempts: "dict[object, int]" = {}
_packet_attempt_order: "list[object]" = []
_PACKET_ATTEMPT_CAP = 8192
# Records are written on the io loop and read/cleared from the Qt thread (the
# Packets dialog), so every registry mutation takes this short lock; it is
# never held across an await.
_packets_lock = threading.Lock()


def _packet_attempt_key(context: PacketContext):
    if context.stream_id is not None:
        return (context.stream_id, context.box_index)
    return (context.kind, context.box_index)


def _box_position(box_index, cap: "bytes | None") -> "int | None":
    """1-based position of ``box_index`` within its stream, using the stream's
    first BACAP counter in the cap's trailing 104-byte index (present in both
    the 168-byte write cap and the 136-byte read cap). None when it can't be
    derived."""
    if box_index is None or not cap or len(cap) < 104:
        return None
    first = int.from_bytes(cap[-104:][:8], "little")
    position = int(box_index) - first + 1
    return position if position >= 1 else None


# Upload labels (agg_bacap_stream -> "basename (in conversation)"), captured at
# send time so a packet retained after the I-chunk is ACK'd (and its DB link
# deleted) can still show the filename. Capped FIFO; a mid-upload restart is
# covered by the Packets/Transfers DB fallback while the I-chunk still exists.
_upload_labels: "dict[object, str]" = {}
_upload_label_order: "list[object]" = []
_UPLOAD_LABEL_CAP = 4096


def set_upload_label(stream_id, label: "str | None") -> None:
    if stream_id is None or not label:
        return
    with _packets_lock:
        if stream_id not in _upload_labels:
            _upload_label_order.append(stream_id)
        _upload_labels[stream_id] = label
        while len(_upload_label_order) > _UPLOAD_LABEL_CAP:
            _upload_labels.pop(_upload_label_order.pop(0), None)


def upload_label(stream_id) -> "str | None":
    with _packets_lock:
        return _upload_labels.get(stream_id)


def _file_marker_basename(payload: bytes) -> "str | None":
    """The basename of a local ``file_outgoing`` marker payload, if it is one."""
    if not payload or payload[:1] != b"F":
        return None
    try:
        decoded = cbor2.loads(payload[1:])
    except Exception:
        return None
    if isinstance(decoded, dict) and decoded.get("kind") == "file_outgoing":
        return decoded.get("basename") or None
    return None


def _next_packet_attempt(context: PacketContext) -> int:
    key = _packet_attempt_key(context)
    if key not in _packet_attempts:
        _packet_attempt_order.append(key)
    attempt = _packet_attempts.get(key, 0) + 1
    _packet_attempts[key] = attempt
    while len(_packet_attempt_order) > _PACKET_ATTEMPT_CAP:
        _packet_attempts.pop(_packet_attempt_order.pop(0), None)
    return attempt


def packet_begin(context: PacketContext, envelope_hash=None) -> str:
    """Record a send starting; returns its id."""
    with _packets_lock:
        packet_id = uuid.uuid4().hex
        _packets[packet_id] = _PacketRecord(packet_id, context, envelope_hash)
        context.packet_id = packet_id
        return packet_id


def packet_finish(packet_id: "str | None", status: str) -> None:
    """Mark a send finished and retain it subject to the limit."""
    if packet_id is None:
        return
    with _packets_lock:
        record = _packets.get(packet_id)
        if record is None:
            return
        record.status = status
        record.finished_at = time.monotonic()
        _packet_finished_order.append(packet_id)
        _prune_finished_packets_locked()


def _prune_finished_packets_locked() -> None:
    while len(_packet_finished_order) > _packet_finished_limit:
        _packets.pop(_packet_finished_order.pop(0), None)


def set_packet_finished_limit(limit: int) -> None:
    """Set how many finished packets to retain (0 drops them all)."""
    global _packet_finished_limit
    with _packets_lock:
        _packet_finished_limit = max(int(limit), 0)
        _prune_finished_packets_locked()


def get_packet_finished_limit() -> int:
    return _packet_finished_limit


def clear_finished_packets() -> None:
    """Drop every finished packet now (the in-flight ones stay)."""
    with _packets_lock:
        for packet_id in _packet_finished_order:
            _packets.pop(packet_id, None)
        _packet_finished_order.clear()


def packets_snapshot() -> "list[dict]":
    """A copy of the live packet records, safe to read from the Qt thread."""
    with _packets_lock:
        return [
            {
                "id": record.id,
                "kind": record.kind,
                "stream_id": record.stream_id,
                "box_index": record.box_index,
                "box_position": record.box_position,
                "attempt": record.attempt,
                "sent_at": record.sent_at,
                "sent_wall": record.sent_wall,
                "timeout_s": record.timeout_s,
                "status": record.status,
                "finished_at": record.finished_at,
                "envelope_hash": record.envelope_hash,
                "stage": record.stage,
                "label": record.label,
            }
            for record in _packets.values()
        ]


def reset_packets() -> None:
    """Drop every record and reset the retention limit (test helper)."""
    global _packet_finished_limit
    with _packets_lock:
        _packets.clear()
        _packet_finished_order.clear()
        _packet_attempts.clear()
        _packet_attempt_order.clear()
        _upload_labels.clear()
        _upload_label_order.clear()
        _packet_finished_limit = DEFAULT_PACKET_FINISHED_LIMIT


def install_stats_counters(connection) -> None:
    """Wrap ``connection``'s encrypt_read/encrypt_write/
    start_resending_encrypted_message so every pigeonhole operation -- on any
    stream kind (normal, substream, voucher) -- is counted.

    The app uses a single long-lived ThinClient, so wrapping the instance
    covers every call site (including voucher.py, which shares the connection)
    without editing them. Idempotent per connection.
    """
    if getattr(connection, "_stats_installed", False):
        return
    connection._stats_installed = True
    original_encrypt_read = connection.encrypt_read
    original_encrypt_write = connection.encrypt_write
    original_start_resending = connection.start_resending_encrypted_message

    async def encrypt_read(*args, **kwargs):
        stats.reads_prepared += 1
        return await original_encrypt_read(*args, **kwargs)

    async def encrypt_write(*args, **kwargs):
        stats.writes_prepared += 1
        return await original_encrypt_write(*args, **kwargs)

    async def start_resending_encrypted_message(*args, **kwargs):
        # Read calls pass read_cap (write_cap=None); write calls pass write_cap.
        context = kwargs.pop("_packet_context", None)
        is_read = kwargs.get("read_cap") is not None
        is_write = kwargs.get("write_cap") is not None
        stats.packets_sent += 1
        stats.packets_in_flight += 1
        if is_read:
            stats.reads_sent += 1
        elif is_write:
            stats.writes_sent += 1
        packet_id = (
            packet_begin(context, kwargs.get("envelope_hash"))
            if context is not None else None
        )
        try:
            result = await original_start_resending(*args, **kwargs)
        except BoxIDNotFoundError:
            if is_read:
                stats.reads_boxnotfound += 1
            packet_finish(packet_id, PACKET_STATUS_BOXNOTFOUND)
            raise
        except (ThinClientOfflineError, BrokenPipeError, OSError):
            # Link-down: the send could not be carried. StartResendingCancelled
            # and courier/epoch/decrypt errors are responses, not link loss.
            stats.packets_link_down += 1
            packet_finish(packet_id, PACKET_STATUS_LINK_DOWN)
            raise
        except asyncio.CancelledError:
            # A race timeout and an external pause both cancel the send; the
            # race sets context.timed_out before cancelling.
            timed_out = context is not None and context.timed_out
            packet_finish(
                packet_id,
                PACKET_STATUS_TIMED_OUT if timed_out else PACKET_STATUS_CANCELLED,
            )
            raise
        except BaseException:
            packet_finish(packet_id, PACKET_STATUS_ERROR)
            raise
        finally:
            stats.packets_in_flight -= 1
        if is_read:
            if getattr(result, "plaintext", b""):
                stats.reads_with_payload += 1
                packet_finish(packet_id, PACKET_STATUS_PAYLOAD)
            else:
                packet_finish(packet_id, PACKET_STATUS_EMPTY)
        elif is_write:
            stats.writes_acked += 1
            packet_finish(packet_id, PACKET_STATUS_ACKED)
        return result

    connection.encrypt_read = encrypt_read
    connection.encrypt_write = encrypt_write
    connection.start_resending_encrypted_message = start_resending_encrypted_message


conversation_update_queue: "Tuple[int,bool]" = asyncio.Queue()  # queue of `int`,which are Conversation.id, when we have written to ConversationLog. the bool is "redraw_only"; when True it only redraws and doesn't grow the model

# Tally events consumed off the receive path, as conversation ids. Pushed only
# after the consuming transaction has committed (same discipline as
# conversation_update_queue/peer_added_queue), so the GUI never refreshes
# against uncommitted TallyState rows. The GUI drains this in a queued
# connection and repaints its poll list / timeline placeholders / tab badge.
tally_update_queue: "Tuple[int]" = asyncio.Queue()

# Peers the local client learned of via an INTRODUCTION announcement, as
# ``(conversation_id, display_name)``. Announced on the io loop by the receive
# path; the GUI appends the name to the contacts tree in its own listener.
peer_added_queue: "Tuple[int,str]" = asyncio.Queue()

# Substream file-transfer progress for the GUI Transfers panel.
# Download events are ``(kind, rcw_id, *extra)``:
#   ("started", rcw_id, conversation_id, total_or_None, parent_name)
#   ("piece",    rcw_id, count, received_bytes)  # pieces and effective bytes so far
#   ("completed", rcw_id)
#   ("paused",   rcw_id)
#   ("resumed",  rcw_id)
#   ("failed",   rcw_id, reason_str)         # unprocessable chunk
# Upload events mirror them under distinct kinds, keyed by the indirection
# ReadCapWAL id:
#   ("upload_started",   rcw_id, conversation_id, total_or_None, total_bytes, name)
#   ("upload_piece",     rcw_id, sent_count, remaining_bytes)
#   ("upload_completed", rcw_id)             # last C/F chunk ACK'd
#   ("upload_paused",    rcw_id)
#   ("upload_resumed",   rcw_id)
# Byte counts are effective payload bytes (the chunk-type prefix and any
# wire/framing overhead excluded).
# Pushed on the io loop where the substream's ReceivedPiece/ReadCapWAL rows are
# written; the GUI's transfers_listener drains it and updates DownloadsModel.
substream_progress_queue: "Tuple[str, ...]" = asyncio.Queue()

__resend_queue: "Set[uuid.UUID]" = set()  # tracks bacap_streams currently in MixWAL
__resend_queue_populated = asyncio.Event() # set after existing MixWAL loaded from disk

# In-flight drain_mixwal_read_single tasks keyed by bacap_stream, so a pause
# or cancel can stop one peer's reads without ending the drain loop.
_inflight_reads: "dict[uuid.UUID, asyncio.Task]" = {}

# In-flight drain_mixwal_write_single tasks keyed by bacap_stream; pause and
# cancel use this to stop the chunk currently being cast.
_inflight_writes: "dict[uuid.UUID, asyncio.Task]" = {}

# Streams whose current write has been ACK'd by the courier. The ACK
# bookkeeping that follows must not be interrupted, so pause and cancel leave
# these writes alone.
_write_acknowledged: "set[uuid.UUID]" = set()

#__plaintextwal_updated = asyncio.Event()
#__plaintextwal_updated.set()
readables_to_mixwal_event = asyncio.Event()
readables_to_mixwal_event.set()
async def signal_readables_to_mixwal():
    readables_to_mixwal_event.set()
resendable_event = asyncio.Event()  # signals send_resendable_plaintexts to check if it can do something
resendable_event.set()
async def check_for_new():
    resendable_event.set()


async def notify_outbound_chat_sent(*, conversation_id, conversation_peer_id,
                                     new_write_caps, db_entries, payload,
                                     final_pwal_id=None, log_id=None):
    """Append an outbound chat message's WAL rows/log entry and wake the
    receive-side listeners, all in one io-loop hop.

    Combines what would otherwise be three separate run_in_io round trips
    from the GUI thread (append, queue-put, check_for_new) into one; each
    hop is a real cross-thread future wait. ``log_id`` optionally pre-assigns
    the ConversationLog primary key (see ``persistent.append_outbound_chat``).
    """
    upload = await persistent.append_outbound_chat(
        conversation_id=conversation_id,
        conversation_peer_id=conversation_peer_id,
        new_write_caps=new_write_caps,
        db_entries=db_entries,
        payload=payload,
        final_pwal_id=final_pwal_id,
        log_id=log_id,
    )
    await conversation_update_queue.put((conversation_id, False))
    if upload is not None:
        # A substream file transfer is committed; the Transfers panel tracks
        # it until the last C/F chunk is ACK'd (see drain_mixwal_write_single).
        # Capture the filename label now: once the I-chunk is ACK'd its row is
        # deleted and nothing links the agg stream to the log row.
        basename = _file_marker_basename(payload)
        label = (
            f"{basename} (in {upload.parent_name})"
            if basename else upload.parent_name
        )
        set_upload_label(upload.stream_id, label)
        substream_progress_queue.put_nowait((
            "upload_started", upload.rcw_id, upload.conversation_id,
            upload.total_chunks, upload.total_bytes, upload.parent_name,
            basename,
        ))
    await check_for_new()


__mixwal_updated = asyncio.Event()
__mixwal_updated.set()
__mixnet_connected = asyncio.Event()
_last_connected: "bool | None" = None  # tracks the previous on_connection_status
                                        # report, so transition-only logging
                                        # doesn't warn on every failed retry.

# Set (and replaced with a fresh instance) each time the daemon reconnects
# after having been seen disconnected. A read that captures the current
# instance before waiting can tell whether a reconnect happened *during*
# its wait by checking whether its captured instance later fires, without
# racing a new waiter that starts after the transition (see
# drain_mixwal_read_single's watchdog).
_reconnect_event = asyncio.Event()

# Same swap-on-transition pattern as _reconnect_event, but for PKI epoch
# rollovers: start_resending_encrypted_message's envelope is only valid for
# the epoch it was encrypted under (see voucher.py's _read_box docstring),
# so a read that spans a rollover needs to notice and re-encrypt with a
# fresh envelope rather than let the daemon's own ride-out keep retrying an
# envelope the courier will reject forever.
_last_epoch: "int | None" = None
_epoch_event = asyncio.Event()


async def on_new_pki_document(event: "Dict[str, Any]") -> None:
    """Bump _epoch_event on every epoch advance.

    Parses the epoch out of the raw event ourselves (rather than going
    through connection.pki_document(), which needs a ThinClient instance
    this module-level callback doesn't have a handle on) — the same
    cbor2.loads(event["payload"]) the thin client library itself does in
    parse_pki_doc, called just before this callback fires.
    """
    global _last_epoch, _epoch_event
    try:
        doc = cbor2.loads(event["payload"])
    except Exception as e:
        logger.debug("on_new_pki_document: could not parse event payload: %s", e)
        return
    epoch = doc.get("Epoch")
    if epoch is None or epoch == _last_epoch:
        return
    previous, _last_epoch = _last_epoch, epoch
    logger.info("PKI epoch advanced to %s (from %s)", epoch, previous)
    old_event, _epoch_event = _epoch_event, asyncio.Event()
    old_event.set()


# ---------------------------------------------------------------------------
# PKI consensus summary (Network consensus dialog)
# ---------------------------------------------------------------------------

# The katzenpost epoch origin (core/epochtime/time.go). Epoch numbers are
# floor((now - origin) / Period), so the Period can be recovered from the
# current epoch and the wall clock. Epoch numbers are in the millions, so
# clock skew shifts the recovered Period by microseconds.
KATZENPOST_EPOCH_ORIGIN = datetime(2017, 6, 1, tzinfo=timezone.utc)


def derive_epoch_period_seconds(
    epoch: "int | None", now: "datetime | None" = None,
) -> "int | None":
    """Recover the network's epoch duration in seconds from the current epoch.

    ``None`` when the epoch is unknown or non-positive."""
    if epoch is None or epoch <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    elapsed = (now - KATZENPOST_EPOCH_ORIGIN).total_seconds()
    return int(round(elapsed / epoch))


def format_duration(seconds: "int | None") -> str:
    """Human-readable duration, e.g. ``3y 5d`` / ``2h 5m`` / ``12s``."""
    if seconds is None:
        return "unknown"
    seconds = max(int(seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        years, days = divmod(days, 365)
        if years:
            return f"{years}y {days}d"
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class ConsensusNode(NamedTuple):
    name: str
    addresses: "list[str]"


class ConsensusSummary(NamedTuple):
    """The parts of a PKI document the Network consensus dialog shows.

    ``period_seconds`` is derived from the epoch origin, not carried by the
    document; ``consensus_seconds`` is ``(epoch - genesis_epoch) * period``.
    """
    epoch: int
    genesis_epoch: int
    period_seconds: "int | None"
    mix_layers: "list[list[ConsensusNode]]"
    gateways: "list[ConsensusNode]"
    service_nodes: "list[ConsensusNode]"
    storage_replicas: "list[ConsensusNode]"

    @property
    def epochs_elapsed(self) -> int:
        return max(self.epoch - self.genesis_epoch, 0)

    @property
    def consensus_seconds(self) -> "int | None":
        if self.period_seconds is None:
            return None
        return self.epochs_elapsed * self.period_seconds


def _node_from_descriptor(desc: "dict") -> ConsensusNode:
    addresses = [
        addr if "://" in addr else f"{transport}://{addr}"
        for transport, addrs in (desc.get("Addresses") or {}).items()
        for addr in addrs
    ]
    return ConsensusNode(name=str(desc.get("Name") or "?"), addresses=addresses)


def _decode_nodes(entries) -> "list[ConsensusNode]":
    """Decode the node descriptors the PKI document carries.

    The daemon strips the document's signatures and cert wrapper before
    forwarding it; mix, gateway and service entries are CBOR byte strings
    (see the thin client's pretty_print_pki_doc), while storage replicas
    arrive already decoded as maps."""
    nodes = []
    for entry in entries or []:
        if isinstance(entry, dict):
            desc = entry
        else:
            try:
                desc = cbor2.loads(entry)
            except Exception:
                continue
        if isinstance(desc, dict):
            nodes.append(_node_from_descriptor(desc))
    return nodes


def summarize_pki_document(
    doc, now: "datetime | None" = None,
) -> "ConsensusSummary | None":
    """Summarize a parsed PKI document, or None when none is available."""
    if not doc:
        return None
    epoch = int(doc.get("Epoch") or 0)
    genesis_epoch = int(doc.get("GenesisEpoch") or 0)
    return ConsensusSummary(
        epoch=epoch,
        genesis_epoch=genesis_epoch,
        period_seconds=derive_epoch_period_seconds(epoch, now),
        mix_layers=[
            _decode_nodes(layer) for layer in (doc.get("Topology") or [])
        ],
        gateways=_decode_nodes(doc.get("GatewayNodes") or []),
        service_nodes=_decode_nodes(doc.get("ServiceNodes") or []),
        storage_replicas=_decode_nodes(doc.get("StorageReplicas") or []),
    )


async def get_pki_document(connection):
    """Snapshot the daemon's current parsed PKI document (io loop only)."""
    return connection.pki_document()


def _is_transient_sqlite_busy(exc: OperationalError) -> bool:
    """True for sqlite's own lock-contention error, false for anything else
    (schema drift, a malformed database, a readonly filesystem) that also
    happens to raise sqlalchemy.exc.OperationalError. Only the former should
    be treated as "retry later"; the latter is an invariant bug and ought to
    stay loud instead of retrying forever."""
    return "database is locked" in str(exc.orig).lower()

def _is_duplicate_arming(exc: "OperationalError | IntegrityError") -> bool:
    """True when a pass tried to arm a read whose stream already has a MixWAL
    row. The row is already there, so the pass has nothing to add and the next
    sweep re-selects whatever still needs arming."""
    return "unique constraint failed: mixwal.bacap_stream" in str(
        exc.orig
    ).lower()


__on_message_queues: "Dict[bytes, asyncio.Queue]" = {}

__should_quit = asyncio.Event()
def shutdown():
    __should_quit.set()

async def _cancel_and_join(
    tasks: Iterable[asyncio.Task[object]],
) -> None:
    owned = tuple(tasks)
    for task in owned:
        if not task.done() and not task.cancelling():
            task.cancel()
    joined = asyncio.gather(*owned, return_exceptions=True)
    cancelled = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            cancelled = True
    joined.result()
    for task in owned:
        if not task.cancelled():
            task.result()
    if cancelled:
        raise asyncio.CancelledError


async def start_background_threads(connection: ThinClient) -> None:
    """Run the network workers and join their requests before returning."""
    install_stats_counters(connection)
    workers: list[asyncio.Task[None]] = [
        create_task(_supervised(provision_read_caps, connection)),
    ]
    stopping = asyncio.create_task(__should_quit.wait())
    try:
        if not await _wait_for_connection_or_shutdown():
            return
        workers.extend((
            create_task(_supervised(drain_mixwal, connection)),
            create_task(_supervised(send_resendable_plaintexts, connection)),
            create_task(readables_to_mixwal_supervised(connection)),
        ))
        done, _ = await asyncio.wait(
            [*workers, stopping], return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
        if stopping not in done:
            # A worker returning while we are not shutting down is a bug, not
            # a shutdown. Surfacing it here beats tearing the stack down
            # silently: the caller owns the io loop, and in the GUI that loop
            # stopping wedges every later run_in_io forever.
            logger.critical(
                "network worker returned without a shutdown request; "
                "stopping the stack",
            )
    finally:
        await _cancel_and_join((*workers, stopping))

def _failure_reason(exc: BaseException) -> str:
    """A bounded, printable reason for a failed received transfer.

    The exception is raised while parsing second-party content, so its text
    can embed peer-chosen bytes of any length. The reason is persisted and
    rendered in the transfers panel, so keep only the exception type. The
    full exception is already logged with a traceback.
    """
    return type(exc).__name__


async def drain_mixwal(connection: ThinClient):
    await drain_mixwal2(connection)


async def _remint_mixwal(mw: persistent.MixWAL, fresh) -> bool:
    """Persist a freshly minted envelope onto an existing MixWAL row.

    fresh is an EncryptWriteResult. Returns False if the row no longer
    exists (a concurrent path deleted it). The mw we were handed is
    detached from the scheduler's closed session, so re-fetch by primary
    key rather than sess.add()ing the stale object.
    """
    async with persistent.asession() as sess:
        row = await sess.get(persistent.MixWAL, mw.id)
        if row is None:
            return False
        row.envelope_hash = fresh.envelope_hash
        row.encrypted_payload = fresh.message_ciphertext
        row.envelope_descriptor = fresh.envelope_descriptor
        row.next_message_index = fresh.next_message_box_index
        sess.add(row)
        await sess.commit()
    return True


async def persist_first_unread(conversation_id: int, first_unread: int) -> None:
    """Persist a conversation's first_unread cursor; io-loop only.

    All conversation writes funnel through this loop's single aiosqlite
    writer, so callers (Qt-side refresh paths) schedule this via
    run_in_io rather than opening a session here themselves."""
    async with persistent.asession() as sess:
        row = await sess.get(persistent.Conversation, conversation_id)
        if row is None:
            logger.warning(
                "persist_first_unread: conversation %d not found; skipping",
                conversation_id,
            )
            return
        row.first_unread = first_unread
        sess.add(row)
        await sess.commit()


async def _remint_write_envelope(connection: ThinClient, mw: persistent.MixWAL, wcw: persistent.WriteCapWAL) -> bool:
    """Re-encrypt a stale write envelope at the same index, from the
    PlaintextWAL payload that is retained until the write is ACK'ed."""
    pwal = None
    if mw.plaintextwal is not None:
        async with persistent.asession() as sess:
            pwal = await sess.get(persistent.PlaintextWAL, mw.plaintextwal)
    if pwal is None:
        # Must drop the row: bacap_stream is unique, so keeping one we can
        # never re-mint blocks every later write on this stream.
        logger.critical(
            "cannot re-mint write for stream %s: PlaintextWAL %s missing; "
            "dropping the MixWAL row", mw.bacap_stream, mw.plaintextwal)
        async with persistent.asession() as sess:
            if row := await sess.get(persistent.MixWAL, mw.id):
                await sess.delete(row)
                await sess.commit()
        return False
    try:
        fresh = await _rpc_racing_connection_life(
            bacap_uuid=mw.bacap_stream,
            what="encrypt_write",
            rpc_factory=lambda: connection.encrypt_write(
                plaintext=pwal.bacap_payload,
                write_cap=wcw.write_cap,
                message_box_index=mw.current_message_index),
            backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
        )
    except _REMINT_TRANSIENT_ERRORS as e:
        logger.warning("re-mint encrypt_write failed, will retry: %s", e)
        return False
    return await _remint_mixwal(mw, fresh)


async def drain_mixwal_write_single(connection:ThinClient, mw: persistent.MixWAL, draining_right_now: "set[uuid.UUID]") -> None:
    """Resend a write until it is ACK'ed by courier.

    A link drop mid-attempt surfaces as ThinClientOfflineError and is handed
    back to the surrounding drain loop (give_up) rather than killing this
    task; the loop re-sweeps once the connection is back.
    """
    from sqlmodel import select

    def give_up() -> None:
        """Release the stream so the drain loop can schedule it again."""
        draining_right_now.discard(mw.bacap_stream)
        # leave it in __resend_queue so we don't skip ahead in the stream.
        # __mixwal_updated.set() makes the retry prompt instead of waiting
        # the drain loop's 15s sweep.
        __mixwal_updated.set()
    async with persistent.asession() as sess:
        wcw = (await sess.exec(select(persistent.WriteCapWAL).where(persistent.WriteCapWAL.id==mw.bacap_stream))).one()
        if mw.plaintextwal is not None and await sess.get(
            persistent.PlaintextWAL, mw.plaintextwal,
        ) is None:
            # The PlaintextWAL row this envelope was built from is gone (a
            # cancelled upload); drop the orphaned MixWAL row instead of
            # sending it.
            row = await sess.get(persistent.MixWAL, mw.id)
            if row is not None:
                await sess.delete(row)
                await sess.commit()
            give_up()
            return
    packet_context = PacketContext(
        "write",
        stream_id=mw.bacap_stream,
        box_index=int.from_bytes(mw.current_message_index[:8], "little"),
        box_position=_box_position(
            int.from_bytes(mw.current_message_index[:8], "little"),
            wcw.write_cap,
        ),
        timeout_s=READ_WATCHDOG_SECONDS,
        label=upload_label(mw.bacap_stream),
    )
    try:
      resp = await _delivery_racing_connection_life(
          bacap_uuid=mw.bacap_stream,
          what="start_resending_encrypted_message",
          rpc_factory=lambda: connection.start_resending_encrypted_message(
              write_cap=wcw.write_cap,
              envelope_descriptor=mw.envelope_descriptor, envelope_hash=mw.envelope_hash,
              message_ciphertext=mw.encrypted_payload,
              read_cap=None, message_box_index=None, reply_index=None,
              _packet_context=packet_context,
          ),
          packet_context=packet_context,
      )
    except ConnectionLifeInterruptedError as e:
      # A reconnect or epoch rollover interrupted the RPC above; the request
      # may have reached the daemon without its reply. The envelope was not
      # ACK'd, so leave the MixWAL row for the next drain pass to re-send
      # (idempotent by envelope hash).
      logger.warning(
          "thin client reconnected or epoch rolled over mid-write RPC for "
          "bacap_stream=%s (%s); leaving MixWAL row for the next drain pass",
          mw.bacap_stream, e,
      )
      give_up()
      return
    except (ThinClientOfflineError, BrokenPipeError, StartResendingCancelledError, OSError) as e:
      # OSError (e.g. a stale socket's "Bad file descriptor" right after a
      # daemon reconnect) is included here to match the equivalent read-path
      # except clause; the sleep before give_up() matches it too, so a
      # persistent (non-transient) failure of this kind backs off instead of
      # retrying in a tight loop.
      logger.warning("thin client is offline or resend cancelled, can't drain mixwal: %s", e)
      await asyncio.sleep(5)
      give_up()
      return
    except CourierInvalidEpochError as e:
      # Permanent for the stored blob (rotated replica keys); re-mint from
      # the retained plaintext and let the scheduler resend. Sleep even on
      # success so a daemon holding a stale PKI document cannot hot-loop.
      logger.warning(
          "drain_mixwal_write_single: stale replica epoch (%s); re-minting envelope", e,
      )
      await _remint_write_envelope(connection, mw, wcw)
      await asyncio.sleep(5)
      give_up()
      return
    except CourierError as e:
      logger.warning(
          "drain_mixwal_write_single: courier rejected envelope (%s); will retry", e,
      )
      await asyncio.sleep(5)
      give_up()
      return

    logger.info(f"drain_mixwal_write_single got resp: {resp}")

    """if there's no error:
    - add to Sentlog so we stop resending,
    - remove from MixWAL,
    - remove from PlaintextWAL?
    - bump send_resendable,
    - bump drain_mixwal
    """
    async def resolve_counter(index: bytes) -> int:
      return await _rpc_racing_connection_life(
          bacap_uuid=mw.bacap_stream,
          what="get_message_box_index_counter",
          rpc_factory=lambda: connection.get_message_box_index_counter(index),
          backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
      )

    _write_acknowledged.add(mw.bacap_stream)
    bookkeeping = asyncio.ensure_future(persistent.SentLog.mark_sent(
        connection, mw, __resend_queue, resolve_counter=resolve_counter,
    ))
    try:
      conv_id = await asyncio.shield(bookkeeping)
    except asyncio.CancelledError:
      # A pause or cancel arrived after the courier ACK. Finish the ACK
      # bookkeeping before honouring it, so the MixWAL row and PlaintextWAL
      # rows are left in a consistent state.
      try:
          await bookkeeping
      except Exception:
          logger.exception(
              "drain_mixwal_write_single: ACK bookkeeping failed after "
              "cancellation for bacap_stream=%s", mw.bacap_stream,
          )
      raise
    except ConnectionLifeInterruptedError:
      # The courier ACK is secured; mark_sent only does local bookkeeping.
      # Leave the MW for the next drain pass to re-send the already-ACKed
      # envelope and complete the ACK, as in the sqlite-busy branch below.
      logger.warning(
          "drain_mixwal_write_single: connection-life signal interrupted ACK "
          "bookkeeping for bacap_stream=%s; leaving MW for the next drain pass",
          mw.bacap_stream,
      )
      give_up()
      return
    except OperationalError as e:
      if not _is_transient_sqlite_busy(e):
          raise
      # sqlite write lock contention: the MW was not consumed, only mark_sent
      # failed. Hand the stream back so the sweep re-sends it and finalizes
      # the ACK.
      logger.warning(
          "drain_mixwal_write_single: sqlite busy committing ACK for "
          "bacap_stream=%s; leaving MW for next drain pass", mw.bacap_stream,
      )
      give_up()
      return
    draining_right_now.discard(mw.bacap_stream)  # ready to send
    resendable_event.set()  # signal send_resendable_plaintexts
    __mixwal_updated.set()  # ought to be set
    if conv_id:
        # update the UX:
        create_task(conversation_update_queue.put((conv_id, True)))
    progress = await persistent.upload_progress_after_ack(mw.bacap_stream)
    if progress is not None:
        # Mirror an outbound substream's chunk progress into the Transfers
        # panel; the row is done when the last C/F chunk is ACK'd, which is
        # when the gated I-chunk becomes dispatchable.
        if progress.sent >= progress.total:
            substream_progress_queue.put_nowait(("upload_completed", progress.rcw_id))
        else:
            substream_progress_queue.put_nowait(
                ("upload_piece", progress.rcw_id, progress.sent,
                 progress.remaining_bytes),
            )

_SUBSTREAM_NAME_PREFIX = models.SUBSTREAM_NAME_PREFIX

# Backstop bound on how long a single read's stop-and-wait ARQ may block with
# NO other signal before we abort it at the daemon and re-cast the box. This
# is deliberately generous: kpclientd's own BoxIDNotFound ride-out is
# uncapped by design (a conversation can sit idle, waiting for the peer to
# write anything, for a long time — that is not a hang), so a short flat
# timeout here would fire on every ordinary quiet conversation instead of
# only on a genuinely stuck reply. The reconnect-triggered path below is the
# primary defence and fires much sooner; this is only the last resort if a
# read never gets a reply AND never observes a reconnect either.
READ_WATCHDOG_SECONDS = 1200.0

# How long to give an in-flight read's reply after a mid-wait daemon
# reconnect OR a PKI epoch rollover, before treating it as lost. Both are
# concrete signals that a reply could have been orphaned or the envelope
# gone stale: kpclientd's reconnect-replay can deliver the courier's reply
# to a query_id whose original listener (this call) already gave up
# waiting on the old connection; an epoch rollover makes the courier
# reject the (now-stale) envelope outright.
#
# These are local daemon RPCs that can legitimately queue behind other
# workers sharing one kpclientd; the grace is sized to absorb that
# contention while staying far short of READ_WATCHDOG_SECONDS, which is
# for a genuine network round-trip.
_RECONNECT_GRACE_SECONDS = 90.0
_DAEMON_RPC_TIMEOUT_SECONDS = 90.0

# How long readables_to_mixwal() and send_resendable_plaintexts() wait at
# the __mixnet_connected gate before proceeding via per-item error handling.
# Covers the case where on_connection_status never re-sets the latch (a
# kpclientd restart can reconnect below the callback layer).
_CONNECTION_IDLE_RETRY_S = 60.0

# Cadence at which readables_to_mixwal() runs an arming pass when
# readables_to_mixwal_event is never re-set (e.g. while the daemon is down).
_ARMING_SWEEP_S = 60.0


_EPOCH_LOSS_STREAK: dict[Hashable, int] = {}
_EPOCH_RACE_MAX_LOSSES = 3
_RpcResult = TypeVar("_RpcResult")
_Reply_co = TypeVar("_Reply_co", covariant=True)


class _ResendingArguments(TypedDict, total=False):
    read_cap: bytes | None
    write_cap: bytes | None
    message_box_index: bytes | None
    reply_index: int | None
    envelope_descriptor: bytes | None
    message_ciphertext: bytes | None
    envelope_hash: bytes | None
    no_retry_on_box_id_not_found: bool
    no_idempotent_box_already_exists: bool


class _ResendingClient(Protocol[_Reply_co]):
    def start_resending_encrypted_message(
        self, **kwargs: Unpack[_ResendingArguments],
    ) -> Awaitable[_Reply_co]: ...


class ConnectionLifeInterruptedError(Exception):
    """An in-flight thinclient RPC was interrupted by a daemon reconnect or a
    PKI epoch rollover, so its reply may never arrive. Callers treat it as a
    transient failure: release the stream and let the drain loop re-cast
    (a fresh envelope for reads, the same idempotent envelope for writes)."""

    def __init__(
        self, message: str, *,
        reason: Literal[
            "unknown", "epoch", "reconnect", "backstop"
        ] = "unknown",
        elapsed_s: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.elapsed_s = elapsed_s


_REMINT_TRANSIENT_ERRORS: "tuple[type[Exception], ...]" = (
    ThinClientOfflineError, BrokenPipeError, CourierError, ReplicaError,
    StartResendingCancelledError, ConnectionLifeInterruptedError,
)


def _retryable_rpc_error(exc: BaseException) -> bool:
    if isinstance(exc, _REMINT_TRANSIENT_ERRORS):
        return True
    if isinstance(exc, TimeoutError):
        return isinstance(exc.__cause__, ConnectionLifeInterruptedError)
    return isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN, errno.ECONNABORTED, errno.ECONNREFUSED,
        errno.ECONNRESET, errno.EHOSTUNREACH, errno.ENETDOWN,
        errno.ENETUNREACH, errno.EPIPE, errno.ETIMEDOUT,
    }


async def _rpc_racing_connection_life(
    *, bacap_uuid: Hashable, what: str,
    rpc_factory: Callable[[], Awaitable[_RpcResult]],
    backstop_s: float = READ_WATCHDOG_SECONDS,
    grace_s: float | None = None,
    reconnect_marker: asyncio.Event | None = None,
    epoch_marker: asyncio.Event | None = None,
    packet_context: PacketContext | None = None,
) -> _RpcResult:
    """Await an RPC, racing it against the daemon-reconnect and PKI-epoch
    signals rather than a flat clock.

    If a connection-life signal fires while the RPC is in flight, its reply
    may never arrive; we give it ``grace_s`` more to answer, then raise
    :class:`ConnectionLifeInterruptedError` for the caller's give-up-and-
    re-cast recovery path. ``backstop_s`` bounds the wait when no signal
    ever fires; a signal retains its separately configured grace period.

    ``packet_context`` marks the RPC as a pigeonhole packet send; when the
    race is lost it sets ``packet_context.timed_out`` (read by the send
    wrapper) and counts a timed-out packet for the Mixnet-status Stats
    window. It must stay None for the encrypt_*/counter/control RPCs that are
    not sends.

    Returns the RPC's result unless it raised on its own (that exception
    propagates) or the race was lost.
    """
    if grace_s is None:
        grace_s = _RECONNECT_GRACE_SECONDS
    if reconnect_marker is None:
        reconnect_marker = _reconnect_event
    if epoch_marker is None:
        epoch_marker = _epoch_event
    losses = _EPOCH_LOSS_STREAK.get(bacap_uuid, 0)
    race_epoch = losses < _EPOCH_RACE_MAX_LOSSES
    started = asyncio.get_running_loop().time()
    task = asyncio.ensure_future(rpc_factory())
    reconnect_wait = asyncio.ensure_future(reconnect_marker.wait())
    epoch_wait = asyncio.ensure_future(epoch_marker.wait())
    racing: set[asyncio.Future[_RpcResult] | asyncio.Future[bool]] = {
        task, reconnect_wait,
    }
    if race_epoch:
        racing.add(epoch_wait)
    else:
        logger.warning(
            "%s for bacap_stream=%s lost %d rollovers in a row; letting this "
            "attempt run to the %s s backstop instead of racing the epoch",
            what, bacap_uuid, losses, backstop_s,
        )
    try:
        done, _pending = await asyncio.wait(
            racing, timeout=backstop_s, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()
        reason: Literal["epoch", "reconnect", "backstop"] = "backstop"
        if reconnect_wait in done or epoch_wait in done:
            reason = "reconnect" if reconnect_wait in done else "epoch"
            logger.warning(
                "%s mid-%s for bacap_stream=%s; giving the in-flight call "
                "%.1f s grace (no-signal backstop %.1f s)",
                ("daemon reconnected" if reason == "reconnect"
                 else "PKI epoch rolled over"),
                what, bacap_uuid, grace_s, backstop_s,
            )
            grace_done, _grace_pending = await asyncio.wait(
                {task}, timeout=grace_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in grace_done:
                return task.result()
            if reason == "epoch":
                _EPOCH_LOSS_STREAK[bacap_uuid] = losses + 1
        task.cancel()
        if packet_context is not None:
            packet_context.timed_out = True
            stats.packets_timed_out += 1
        elapsed = asyncio.get_running_loop().time() - started
        raise ConnectionLifeInterruptedError(
            f"{what} for bacap_stream={bacap_uuid} interrupted by {reason} "
            f"after {elapsed:.1f} s (backstop {backstop_s} s)",
            reason=reason, elapsed_s=elapsed,
        )
    finally:
        for owned in (task, reconnect_wait, epoch_wait):
            owned.cancel()
        await asyncio.gather(task, reconnect_wait, epoch_wait, return_exceptions=True)


async def _delivery_racing_connection_life(
    *, bacap_uuid: Hashable, what: str,
    rpc_factory: Callable[[], Awaitable[_RpcResult]],
    backstop_s: float = READ_WATCHDOG_SECONDS,
    grace_s: float | None = None,
    reconnect_marker: asyncio.Event | None = None,
    epoch_marker: asyncio.Event | None = None,
    packet_context: PacketContext | None = None,
) -> _RpcResult:
    result = await _rpc_racing_connection_life(
        bacap_uuid=bacap_uuid,
        what=what,
        rpc_factory=rpc_factory,
        backstop_s=backstop_s,
        grace_s=grace_s,
        reconnect_marker=reconnect_marker,
        epoch_marker=epoch_marker,
        packet_context=packet_context,
    )
    _EPOCH_LOSS_STREAK.pop(bacap_uuid, None)
    return result


async def _await_read_reply(
    connection: _ResendingClient[_RpcResult], *,
    read_watchdog_s: float,
    reconnect_grace_s: float,
    bacap_uuid: Hashable,
    reconnect_marker: asyncio.Event | None = None,
    epoch_marker: asyncio.Event | None = None,
    packet_context: PacketContext | None = None,
    **kwargs: Unpack[_ResendingArguments],
) -> _RpcResult:
    """Await start_resending_encrypted_message, racing it against a daemon
    reconnect or a PKI epoch rollover rather than a flat clock.

    Either signal means the in-flight envelope could be stale or orphaned:
    a reconnect can deliver a reply to a query_id whose listener already
    gave up (see the reconnect log line below); an epoch rollover makes
    the courier reject the envelope outright, and the daemon's own
    no_retry_on_box_id_not_found=False ride-out swallows that rejection
    into more silent retries on the SAME now-stale envelope rather than
    ever returning (see voucher.py's _read_box docstring). Either way the
    fix is the same: give the in-flight call a short grace period, then
    let the caller's existing TimeoutError recovery path cancel it and
    retry -- drain_mixwal_read_single re-encrypts a fresh envelope on
    every call, so that retry is never stale.

    ``packet_context`` is forwarded to the send wrapper (as the private
    ``_packet_context`` kwarg) and to the race, so the Packets window records
    this read and labels a lost race as a timeout.

    Returns the reply, or raises whatever the call itself raised, or raises
    asyncio.TimeoutError (for the caller's existing recovery path) if
    either the grace period or the backstop elapses first.
    """
    if packet_context is not None:
        kwargs.setdefault("_packet_context", packet_context)
    try:
        return await _delivery_racing_connection_life(
            bacap_uuid=bacap_uuid,
            what="wait",
            rpc_factory=lambda: connection.start_resending_encrypted_message(**kwargs),
            backstop_s=read_watchdog_s,
            grace_s=reconnect_grace_s,
            reconnect_marker=reconnect_marker,
            epoch_marker=epoch_marker,
            packet_context=packet_context,
        )
    except ConnectionLifeInterruptedError as exc:
        raise asyncio.TimeoutError() from exc

def _substream_parent_id(name: str) -> "int | None":
    """Parse the parent peer id out of a ``:substream:<parent_id>:<nonce>``
    name, or None when the name is malformed. Pure, so the sync-seeded
    Transfers panel can resolve a parent without the async engine."""
    parts = name.split(":")
    if len(parts) < 4:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


async def _substream_parent(
    sess: persistent.AsyncSession, name: str,
) -> persistent.ConversationPeer | None:
    """Resolve the parent ConversationPeer a substream peer belongs to.

    A synthetic substream peer is named ``:substream:<parent_id>:<nonce>``.
    Returns the parent peer, or None when the name is malformed or the parent
    no longer exists, so the caller retires the peer instead of raising
    inside the read loop.
    """
    parent_id = _substream_parent_id(name)
    if parent_id is None:
        return None
    return await sess.get(persistent.ConversationPeer, parent_id)

# Cap on attachment size after reassembly. Anything larger is
# logged at WARNING, the bytes are discarded, and a
# ``file_oversized`` marker is committed in place of a real
# ``file_marker``. The cap was set in consultation with the
# operator; it is generous enough for photos, audio clips, and
# short documents.
_ATTACHMENT_HARD_CAP = 200 * 1024 * 1024


def _attachments_root() -> Path:
    """Directory under the state file's parent where assembled
    attachments are spilled."""
    return persistent.state_file.parent / "attachments"


_BASENAME_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-+()[]"
)


def _safe_basename(name: str) -> str:
    """Reduce a peer-supplied name to 7-bit ASCII from the allowlist."""
    cleaned = "".join(c if c in _BASENAME_ALLOWED else "_" for c in (name or ""))
    cleaned = cleaned.lstrip(".")
    return cleaned[:200] or "unnamed"


def _spill_attachment(
    file_upload: "models.GroupChatFileUpload",
    membership_hash: bytes,
    conversation_id: int,
) -> bytes:
    """Write the attachment bytes to disk and return the CBOR marker
    that goes into ``ConversationLog.payload``.

    Files larger than :data:`_ATTACHMENT_HARD_CAP` are dropped and a
    ``file_oversized`` marker is returned instead, so the
    conversation log still records that something arrived without
    consuming the disk.

    The spill happens before the caller's commit, which can still be
    retried (e.g. sqlite lock contention): the filename is derived from the
    content hash, not a fresh random id, so a retry that calls this again
    with the same bytes reuses the same file instead of writing (and
    orphaning) a second copy.
    """
    safe = _safe_basename(file_upload.basename)
    blob = file_upload.payload
    if len(blob) > _ATTACHMENT_HARD_CAP:
        logger.warning(
            "attachment of %d bytes exceeds cap %d; dropping body",
            len(blob), _ATTACHMENT_HARD_CAP,
        )
        return b"F" + cbor2.dumps({
            "v": 0,
            "kind": "file_oversized",
            "size": len(blob),
            "basename": safe,
            "filetype": file_upload.filetype,
            "membership_hash": membership_hash,
        })

    sha = hashlib.sha256(blob).digest()
    conv_dir = _attachments_root() / str(conversation_id)
    conv_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = f"{sha.hex()}-{safe}"
    rel_path = f"attachments/{conversation_id}/{filename}"
    abs_path = persistent.state_file.parent / rel_path

    if not abs_path.exists():
        fd = os.open(
            str(abs_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
    # else: already spilled by an earlier, retried attempt with this same
    # content; reuse it rather than writing (and leaking) another copy.

    marker_fields = {
        "v": 0,
        "kind": "file_marker",
        "basename": safe,
        "filetype": file_upload.filetype,
        "size": len(blob),
        "rel_path": rel_path,
        "sha256": sha,
        "membership_hash": membership_hash,
    }
    if attachment_images.is_image_attachment(file_upload.filetype, safe):
        thumb_rel_path = attachment_images.spill_image_thumbnail(
            conversation_id=conversation_id,
            file_uuid=uuid.uuid4(),
            safe_basename=safe,
            source=blob,
        )
        if thumb_rel_path is not None:
            marker_fields["thumb_rel_path"] = thumb_rel_path
    return b"F" + cbor2.dumps(marker_fields)


async def _get_received_piece(sess, rcw_id: "uuid.UUID", idx_8b: bytes):
    """Lookup helper for the (read_cap, bacap_index) composite primary
    key. Returns ``None`` when no row matches."""
    return (await sess.exec(
        select(persistent.ReceivedPiece).where(
            persistent.ReceivedPiece.read_cap == rcw_id,
            persistent.ReceivedPiece.bacap_index == idx_8b,
        )
    )).first()


async def _try_assemble(sess, rcw_id: "uuid.UUID", terminal_idx_8b: bytes):
    """Walk back from a freshly-inserted ``ReceivedPiece`` and try to
    coalesce a chain.

    Returns:

    * ``("F", chunks, chain, gcm)`` when ``terminal_idx_8b`` resolves
      to a ``b'F'`` chunk and every predecessor down to the substream's
      start (or the previous ``b'F'``/``b'I'`` boundary) is present
      and the concatenated CBOR decodes to a :class:`GroupChatMessage`.
      ``chunks`` is the ordered ``(type, body)`` list passed to
      :func:`models.unserialize`; ``chain`` is the ordered list of
      ``ReceivedPiece`` rows the caller may delete on commit.
    * ``("I", read_cap_bytes, [piece])`` when ``terminal_idx_8b``
      resolves to a ``b'I'`` chunk, whose body is the substream's
      read cap.
    * ``None`` when the chain is still open (no terminator) or has
      gaps that prevent a clean decode.

    BACAP indices are 64-bit little-endian counters stored in the
    first eight bytes of ``MessageBoxIndex`` blobs; predecessors
    are at ``counter - 1``.
    """
    cur = await _get_received_piece(sess, rcw_id, terminal_idx_8b)
    if cur is None:
        return None
    if cur.chunk_type == b"I":
        return ("I", cur.chunk, [cur])
    if cur.chunk_type != b"F":
        return None  # a lone 'C', chain not yet terminated

    chain = [cur]
    counter = int.from_bytes(terminal_idx_8b, "little")
    while counter > 0:
        counter -= 1
        prev = await _get_received_piece(
            sess, rcw_id, counter.to_bytes(8, "little"),
        )
        if prev is None:
            break  # either gap or substream start; let CBOR decode decide
        if prev.chunk_type in (b"F", b"I"):
            break  # boundary with a prior assembled message
        chain.insert(0, prev)

    chunks = [(rp.chunk_type, rp.chunk) for rp in chain]
    try:
        gcm = models.unserialize(chunks)
    except Exception as exc:  # malformed CBOR or framing: leave RPs for retry
        logger.warning(
            "could not assemble chain at rcw=%s terminal=%s: %s",
            rcw_id, terminal_idx_8b.hex(), exc, exc_info=True,
        )
        return None
    if gcm is None:
        return None
    return ("F", chunks, chain, gcm)


def _substream_miss_state(
    started_s: float | None, *, terminal: bool,
    now_s: float, budget_s: float,
) -> tuple[float, str | None]:
    started = now_s if started_s is None else started_s
    if terminal:
        return started, "A required box is tombstoned"
    if now_s - started >= budget_s:
        return started, "A required box remained unavailable"
    return started, None


async def _record_substream_miss(
    bacap_stream: uuid.UUID, *, terminal: bool,
    now_s: float, budget_s: float,
) -> bool:
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
        if rcw is None:
            return False
        started, failure = _substream_miss_state(
            rcw.substream_missing_since, terminal=terminal,
            now_s=now_s, budget_s=budget_s,
        )
        rcw.substream_missing_since = started
        rcw.substream_failure = failure
        sess.add(rcw)
        if failure is not None:
            peers = (await sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.read_cap_id == bacap_stream,
                )
            )).all()
            for peer in peers:
                peer.active = False
                sess.add(peer)
            pending = (await sess.exec(select(persistent.MixWAL).where(
                persistent.MixWAL.bacap_stream == bacap_stream,
                persistent.MixWAL.is_read,
            ))).all()
            for row in pending:
                await sess.delete(row)
        await sess.commit()
    if failure is not None:
        substream_progress_queue.put_nowait(("failed", bacap_stream, failure))
        return True
    return False


async def _discard_substream_release(
    sess: persistent.AsyncSession, parent_cap_id: uuid.UUID,
    read_cap: bytes,
) -> None:
    pieces = (await sess.exec(
        select(persistent.ReceivedPiece).where(
            persistent.ReceivedPiece.read_cap == parent_cap_id,
            persistent.ReceivedPiece.chunk_type == b"I",
            persistent.sa.func.length(
                persistent.ReceivedPiece.chunk,
            ).in_((136, 140)),
            persistent.sa.func.substr(
                persistent.ReceivedPiece.chunk, -136,
            ) == read_cap,
        )
    )).all()
    for piece in pieces:
        await sess.delete(piece)


async def drain_mixwal_read_single(*, connection:ThinClient, rcw_read_cap: bytes, mw: persistent.MixWAL, draining_right_now: "set[uuid.UUID]", read_watchdog_s: float = READ_WATCHDOG_SECONDS, reconnect_grace_s: float = _RECONNECT_GRACE_SECONDS):
  """Given a single persisten.MixWAL with is_read==True:
    - Send it to the network.
    - If we get a response:
      - A message: We can progress
      - A box not found:
        - We should: Resend after the local polling delay
  """
  assert mw.is_read
  assert len(rcw_read_cap) == 136
  bacap_uuid = mw.bacap_stream

  # Substreams retry missing boxes within their read budget. A tombstone
  # or an exhausted budget retires the transfer and reports the failure.
  async with persistent.asession() as _pre_sess:
      _cp_row = (await _pre_sess.exec(
          select(persistent.ConversationPeer).where(
              persistent.ConversationPeer.read_cap_id == mw.bacap_stream,
          )
      )).one_or_none()
      _pre_rcw = await _pre_sess.get(persistent.ReadCapWAL, bacap_uuid)
      if _pre_rcw is not None and _pre_rcw.paused:
          draining_right_now.discard(bacap_uuid)
          return
  is_substream = _cp_row is not None and _cp_row.name.startswith(_SUBSTREAM_NAME_PREFIX)

  def give_up() -> None:
    """Unblocks the mw so it can be scheduled again."""
    draining_right_now.discard(bacap_uuid)
    # we don't clear it from __resend_queue because we don't want to skip
    # ahead in the stream.
    #
    # readables_to_mixwal_event only wakes readables_to_mixwal(), whose query
    # excludes any ReadCapWAL that still has a MixWAL row -- exactly this
    # row's state after a give_up(), so it alone is a no-op for retrying THIS
    # mw. __mixwal_updated is what actually makes drain_mixwal2 re-scan
    # MixWAL.get_new() promptly instead of waiting out its ~15s poll.
    readables_to_mixwal_event.set()
    __mixwal_updated.set()
    return

  reconnect_marker = _reconnect_event
  epoch_marker = _epoch_event
  try:
    # Re-encrypt fresh every call rather than reusing mw's persisted
    # envelope: start_resending_encrypted_message's envelope is only valid
    # for the PKI epoch it was encrypted under, so a stream that's been
    # given up on and retried (any give_up() below, on this call or a
    # previous one) after an epoch rollover must not resend the same now-
    # stale envelope, which the courier would reject forever (see
    # voucher.py's _read_box docstring). mw's own envelope_hash/
    # encrypted_payload/envelope_descriptor/next_message_index columns are
    # left as they were when readables_to_mixwal() first created the row;
    # only this fresh result is ever used for the actual RPC.
    rcr = None
    rcr = await _rpc_racing_connection_life(
        bacap_uuid=bacap_uuid,
        what="encrypt_read",
        rpc_factory=lambda: connection.encrypt_read(
            read_cap=rcw_read_cap, message_box_index=mw.current_message_index,
        ),
        backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
        grace_s=reconnect_grace_s,
        reconnect_marker=reconnect_marker,
        epoch_marker=epoch_marker,
    )
  except (ConnectionLifeInterruptedError, TimeoutError, ThinClientOfflineError, OSError) as exc:
    logger.warning(
        "Read setup failed for %s; retrying: %s", bacap_uuid, exc,
        exc_info=not _retryable_rpc_error(exc),
    )
    await asyncio.sleep(5)
    give_up()
    return

  read_started = asyncio.get_running_loop().time()
  try:
    # Re-read the current globals rather than reuse the markers captured
    # above: if a reconnect or epoch rollover fired during the encrypt_read
    # race just above, on_connection_status/on_new_pki_document already
    # .set() that captured Event and swapped in a fresh one for future
    # waiters. Reusing the stale (permanently-set) reference here would make
    # this second race see it as already-done and wrongly truncate this
    # wait to the short reconnect grace period instead of the intended
    # read_watchdog_s.
    resp = await _await_read_reply(
        connection,
        reconnect_marker=_reconnect_event,
        epoch_marker=_epoch_event,
        read_watchdog_s=read_watchdog_s,
        reconnect_grace_s=reconnect_grace_s,
        bacap_uuid=bacap_uuid,
        read_cap=rcw_read_cap,
        write_cap=None,
        message_box_index=mw.current_message_index,
        reply_index=None,
        envelope_descriptor=rcr.envelope_descriptor,
        envelope_hash=rcr.envelope_hash,
        message_ciphertext=rcr.message_ciphertext,
        no_retry_on_box_id_not_found=True,
        packet_context=PacketContext(
            "substream_read" if is_substream else "contact_read",
            stream_id=bacap_uuid,
            box_index=int.from_bytes(mw.current_message_index[:8], "little"),
            box_position=_box_position(
                int.from_bytes(mw.current_message_index[:8], "little"),
                rcw_read_cap,
            ),
            timeout_s=read_watchdog_s,
        ),
    )
  except ConnectionLifeInterruptedError as e:
    # The fresh encrypt_read was interrupted before dispatching anything, so
    # the daemon's box is untouched; re-encrypting the same message_box_index
    # on the reconnected client is safe. Release the stream so the drain
    # loop re-casts on the next sweep.
    logger.warning(
        "drain_mixwal_read_single: %s; re-scheduling the read for "
        "bacap_stream=%s (nothing was dispatched for this envelope)",
        e, bacap_uuid,
    )
    give_up()
    return
  except asyncio.TimeoutError as exc:
    # No reply within the watchdog: the courier keeps the box and re-serves
    # it, so abort the in-flight ARQ at the daemon and let the drain loop
    # re-cast the same box with a fresh query id.
    logger.warning(
        "drain_mixwal_read_single: read for bacap_stream=%s gave up after"
        " %.1f s (watchdog %s s); cancelling the in-flight ARQ and re-scheduling",
        bacap_uuid, asyncio.get_running_loop().time() - read_started,
        read_watchdog_s, exc_info=not _retryable_rpc_error(exc),
    )
    try:
        await asyncio.wait_for(
            connection.cancel_resending_encrypted_message(rcr.envelope_hash),
            timeout=10,
        )
        logger.debug("drain_mixwal_read_single: cancelled in-flight ARQ for %s", bacap_uuid)
    except asyncio.TimeoutError:
        logger.warning("drain_mixwal_read_single: cancel ARQ did not answer for %s", bacap_uuid)
    except Exception as _cancele:  # pragma: no cover - defensive best-effort
        logger.debug("drain_mixwal_read_single: cancel ARQ best-effort: %s", _cancele)
    await asyncio.sleep(1)
    give_up()
    return
  except asyncio.CancelledError:
    # An external pause cancelled the drain task while the
    # read ARQ was in flight. Cancel the same in-flight ARQ at the daemon
    # so its retransmits stop too, then re-raise so the drain loop's
    # done-callback releases the stream from draining_right_now. The MW
    # row is deleted by the pausing peer, not here, so a resume can.
    if rcr is not None:
      try:
        await asyncio.wait_for(
            connection.cancel_resending_encrypted_message(rcr.envelope_hash),
            timeout=10,
        )
        logger.debug(
            "drain_mixwal_read_single: cancelled in-flight ARQ for %s on "
            "pause", bacap_uuid,
        )
      except Exception as _cancele:  # pragma: no cover - defensive best-effort
        logger.debug(
            "drain_mixwal_read_single: cancel ARQ on pause best-effort: %s",
            _cancele,
        )
    raise
  except (katzenpost_thinclient.core.MKEMDecryptionFailedError,
          BACAPDecryptionFailedError, StartResendingCancelledError,
          ThinClientOfflineError, BrokenPipeError, OSError) as e:
    logger.warning(
        "drain_mixwal_read_single giving up: %s", e,
        exc_info=not _retryable_rpc_error(e),
    )
    await asyncio.sleep(5)
    give_up()
    return
  except (BoxIDNotFoundError, TombstoneError) as e:
    failed = False
    if is_substream:
      failed = await _record_substream_miss(
          bacap_uuid, terminal=isinstance(e, TombstoneError),
          now_s=time.time(), budget_s=read_watchdog_s,
      )
    if not failed:
      logger.debug("read box unavailable; retrying %s: %s", bacap_uuid, e)
      await asyncio.sleep(5)
    give_up()
    return
  except DatabaseFailureError:
    # A storage replica reported a database error from ITS OWN backend store
    # (the replica's RocksDB; ErrFailedDBRead, a deserialise failure, or a
    # momentarily closed DB, see replica/handlers.go handleReplicaRead). This
    # is NOT katzenqt's local SQLite, and (since the daemon now remaps courier
    # errors out of the replica code range) NOT a courier rejection either. The
    # daemon does not retry it, so we back off and reschedule the same read
    # rather than advancing the stream or disabling the conversation.
    logger.warning(
        "drain_mixwal_read_single: a storage replica reported a database error "
        "from its own backend store (not katzenqt's local SQLite); "
        "treating as transient and will retry"
    )
    await asyncio.sleep(5)
    give_up()
    return
  except CourierError as e:
    # A courier-side rejection of the read envelope (e.g. a stale replica epoch,
    # or a malformed/uncacheable envelope), distinct from any replica error and
    # from our local SQLite. The daemon remaps these out of the replica code
    # range precisely so we can tell them apart. Treat as transient and retry.
    # Nothing to re-mint here: every pass re-encrypts a fresh envelope at the
    # top of drain_mixwal_read_single and resends that, never the stored blob.
    logger.warning(
        "drain_mixwal_read_single: the courier rejected the read envelope (%s); "
        "will retry", e,
    )
    await asyncio.sleep(5)
    give_up()
    return

  logger.debug(f"got reply for outbound read mw {resp}")
  assert resp is not None, "outbound read reply is None, but ought to be retrying"
  async def _box_index_counter(index: bytes) -> int:
    return await _rpc_racing_connection_life(
        bacap_uuid=bacap_uuid,
        what="get_message_box_index_counter",
        rpc_factory=lambda: connection.get_message_box_index_counter(index),
        backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
    )

  async with persistent.asession() as sess:
    rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
    idx_old = await _box_index_counter(rcw.next_index)
    idx_new = await _box_index_counter(rcr.next_message_box_index)
    if idx_old >= idx_new:
      logger.warning(f"not advancing idx to {idx_new} from old {idx_old}, we probably already handled this? ought to not be possible.")
      try:
        await sess.delete(mw)
        await sess.commit()
      except Exception as e:  # pragma: no cover - defensive: commit-of-delete should never fail
        logger.critical(
            "error committing deletion of stray MW: %s", e, exc_info=True,
        )
      draining_right_now.discard(bacap_uuid)  # otherwise this stream is wedged forever with no exception needed
      readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
      return
    logger.info(f"advancing read to idx {idx_new}")
    assert idx_new == idx_old + 1, f"idx mismatch {idx_new} != {idx_old} + 1"
    rcw.next_index = rcr.next_message_box_index
    rcw.substream_missing_since = None
    sess.add(rcw)
    chunk_type = resp.plaintext[:1]
    chunk_body = resp.plaintext[1:]
    if chunk_type not in (b"F", b"C", b"I"):
      logger.critical(f"received message with invalid prefix, going to stop reading this peer {resp}")
      cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
      cp.active = False
      sess.add(cp)
      failure = None
      if cp.name.startswith(_SUBSTREAM_NAME_PREFIX):
          failure = "The transfer contains an invalid chunk prefix"
          rcw.substream_failure = failure
          sess.add(rcw)
      await sess.delete(mw)
      await sess.commit()
      if failure is not None:
          substream_progress_queue.put_nowait(("failed", bacap_uuid, failure))
      draining_right_now.discard(bacap_uuid)  # otherwise this stream is wedged forever with no exception needed
      __mixwal_updated.set()
      return

    cp = (await sess.exec(select(persistent.ConversationPeer).where(persistent.ConversationPeer.read_cap_id==rcw.id))).one()
    sess.add(persistent.ReceivedPiece(
                read_cap=mw.bacap_stream,
                bacap_index=mw.current_message_index[:8],
                chunk_type=chunk_type,
                chunk=chunk_body,
            ))

    # Substream progress events, held until the transaction
    # commits and fired for the GUI's transfers_listener. count()/sum() run in
    # the same (unflushed) transaction, so they already include the row
    # just added above -- matching the ReceivedPiece count the Transfers
    # panel shows. The byte sum is the effective payload (ReceivedPiece.chunk
    # has the 1-byte chunk-type prefix already stripped), used for the rate
    # column.
    substream_progress = []
    if cp.name.startswith(_SUBSTREAM_NAME_PREFIX):
        piece_count, received_bytes = (await sess.exec(
            select(
                persistent.sa.func.count(),
                persistent.sa.func.coalesce(
                    persistent.sa.func.sum(
                        persistent.sa.func.length(persistent.ReceivedPiece.chunk),
                    ),
                    0,
                ),
            ).select_from(persistent.ReceivedPiece)
            .where(persistent.ReceivedPiece.read_cap == mw.bacap_stream)
        )).one()
        substream_progress.append((
            "piece", mw.bacap_stream, int(piece_count), int(received_bytes),
        ))

    assembled = await _try_assemble(
        sess, mw.bacap_stream, mw.current_message_index[:8],
    )
    convlog_added = False
    signal_send = False
    peer_added = None
    tally_added = False
    notify_conv_id = cp.conversation.id
    parent_peer = None

    # Resolve where an assembled message's log row will land *before* the
    # writer lock, so the conversation_order assignment below is serialised
    # against concurrent GUI sends (which append on the Qt thread) and other
    # drains of this conversation. Only the F branch appends a log row, but
    # the lock can cheaply cover the whole commit.
    if (
        assembled is not None and assembled[0] == "F"
        and cp.name.startswith(_SUBSTREAM_NAME_PREFIX)
    ):
        parent_peer = await _substream_parent(sess, cp.name)
        if parent_peer is not None:
            notify_conv_id = parent_peer.conversation.id

    # Spill an attachment body before taking the writer lock: hashing and
    # writing a large payload (plus its image thumbnail) can take a while, and
    # holding the per-conversation lock across it starves concurrent appends
    # (a GUI send, a poll create) on the same conversation. The spill is
    # content-hash keyed, so a retried drain reuses the same file.
    spilled_payload = None
    if (
        assembled is not None and assembled[0] == "F"
        and not (cp.name.startswith(_SUBSTREAM_NAME_PREFIX) and parent_peer is None)
    ):
        spill_gcm = assembled[3]
        if spill_gcm.file_upload is not None:
            spill_conv_id = (
                parent_peer.conversation.id
                if cp.name.startswith(_SUBSTREAM_NAME_PREFIX)
                else cp.conversation.id
            )
            spilled_payload = _spill_attachment(
                spill_gcm.file_upload, spill_gcm.membership_hash, spill_conv_id,
            )

    try:
      async with persistent.conversation_log_order_lock(notify_conv_id):
        if assembled is not None and assembled[0] == "F":
            _, chunks, chain, gcm = assembled
            if cp.name.startswith(_SUBSTREAM_NAME_PREFIX) and parent_peer is None:
                logger.warning("retiring substream with no parent: %r", cp.name)
                cp.active = False
                rcw.substream_failure = "The transfer parent no longer exists"
                sess.add(cp)
                sess.add(rcw)
                substream_progress.append((
                    "failed", mw.bacap_stream, rcw.substream_failure,
                ))
            else:
                if gcm.file_upload is not None:
                    # Spilled above, before the lock.
                    full_payload = spilled_payload
                    assert full_payload is not None
                else:
                    if gcm.text is not None:
                        gcm.text = models.clamp_message_text(gcm.text)
                    full_payload = b"F" + gcm.to_cbor()
                if cp.name.startswith(_SUBSTREAM_NAME_PREFIX):
                    # Substream's terminal F: commit the assembled message into the
                    # parent peer's ConversationLog, prune the parent's indirection
                    # piece, and retire this synthetic peer.
                    added, sig, pa, ta = await conversation_handlers.dispatch(sess, parent_peer, gcm, full_payload)
                    signal_send = signal_send or sig
                    peer_added = peer_added or pa
                    tally_added = tally_added or ta
                    await _discard_substream_release(
                        sess, parent_peer.read_cap_id, rcw.read_cap,
                    )
                    cp.active = False
                    rcw.substream_failure = None
                    rcw.substream_missing_since = None
                    rcw.paused = False
                    sess.add(cp)
                    sess.add(rcw)
                    convlog_added = added
                    # The transfer is complete (terminal F
                    # assembled and routed). Held until commit.
                    substream_progress.append(("completed", mw.bacap_stream))
                else:
                    # Top-level F (single-box or contiguous on the parent stream):
                    # route by message type, chat into the log, tally into the
                    # controller.
                    convlog_added, sig, peer_added, tally_added2 = await conversation_handlers.dispatch(sess, cp, gcm, full_payload)
                    signal_send = signal_send or sig
                    tally_added = tally_added or tally_added2
            for rp in chain:
                await sess.delete(rp)

        elif assembled is not None and assembled[0] == "I":
            _, substream_read_cap, _ = assembled
            if len(substream_read_cap) == 136:
                # Legacy 136-byte I-chunk: no total carried; the download
                # renders with an indeterminate denominator.
                new_rcw = persistent.ReadCapWAL(
                    id=uuid.uuid4(),
                    read_cap=substream_read_cap,
                    next_index=substream_read_cap[-104:],
                )
            elif len(substream_read_cap) == 140:
                # Extended I-chunk: bytes 0-3 carry the total
                # plaintext chunk count (C-chunks + final F), bytes 4-139 are
                # the 136-byte read cap. Parse defensively: an out-of-range
                # count still just means indeterminate progress, never a crash.
                total_chunks = struct.unpack(">I", substream_read_cap[:4])[0]
                read_cap_bytes = substream_read_cap[4:]
                new_rcw = persistent.ReadCapWAL(
                    id=uuid.uuid4(),
                    read_cap=read_cap_bytes,
                    next_index=read_cap_bytes[-104:],
                    substream_total_chunks=total_chunks,
                )
            else:
                logger.warning(
                    "ignoring indirection with malformed read cap length %d",
                    len(substream_read_cap),
                )
            # The synthetic peer name embeds the parent peer id, so the name
            # prefix alone scopes this to one announcer.
            open_substreams = len((await sess.exec(
                select(persistent.ConversationPeer).where(
                    persistent.ConversationPeer.active == True,  # noqa: E712
                    persistent.ConversationPeer.name.startswith(
                        f"{_SUBSTREAM_NAME_PREFIX}{cp.id}:",
                    ),
                )
            )).all())
            if len(substream_read_cap) in (136, 140):
                over_cap = open_substreams >= _MAX_OPEN_SUBSTREAMS_PER_PEER
                if over_cap:
                    # Record it as a failed transfer rather than discarding
                    # it. The peer is never armed, so it costs no mixnet
                    # reads, but the user can see that a transfer arrived and
                    # was refused instead of it vanishing with a log line.
                    logger.warning(
                        "peer %s already has %d open substreams; refusing the "
                        "indirection", cp.id, open_substreams,
                    )
                    new_rcw.substream_failure = _OVER_CAP_FAILURE
                sess.add(new_rcw)
                substream_peer = persistent.ConversationPeer(
                    name=f"{_SUBSTREAM_NAME_PREFIX}{cp.id}:{secrets.token_hex(2)}",
                    read_cap_id=new_rcw.id,
                    active=not over_cap,
                    conversation=cp.conversation,
                )
                sess.add(substream_peer)
                # Announce the new download. Held until commit.
                substream_progress.append((
                    "started", new_rcw.id, cp.conversation.id,
                    new_rcw.substream_total_chunks,
                    cp.name,
                ))
                if over_cap:
                    substream_progress.append((
                        "failed", new_rcw.id, _OVER_CAP_FAILURE,
                    ))

        await sess.delete(mw)
        bacap_uuid = mw.bacap_stream
        await sess.commit()
    except OperationalError as e:
      if not _is_transient_sqlite_busy(e):
          raise
      # sqlite write lock contention committing a received message (e.g. a
      # concurrent GUI-send commit on the same file). Discard the uncommitted
      # transaction by giving up: the enclosing asession() unwinds on return
      # and rolls back the rcw advance / ReceivedPiece adds / MW delete, so
      # the next pass re-reads from the same index with no duplicate or gap.
      # Only wrapping OperationalError keeps invariant bugs (IntegrityError
      # on conversation_order, etc.) loud.
      logger.warning(
          "drain_mixwal_read_single: sqlite busy committing received message "
          "for bacap_stream=%s; leaving MW for next drain pass", bacap_uuid,
      )
      give_up()
      return
    except Exception as e:
      logger.error("Received-message handler failed: %s", e, exc_info=True)
      # Check if this is a substream peer (look up the peer by bacap_stream)
      async with persistent.asession() as _cp_sess:
          _cp = (await _cp_sess.exec(select(persistent.ConversationPeer).where(
              persistent.ConversationPeer.read_cap_id == mw.bacap_stream,
          ))).first()
      is_substream = _cp is not None and _cp.name.startswith(_SUBSTREAM_NAME_PREFIX)
      
      if is_substream:
          # Substream: deactivate peer and fire failed event. Rollback the
          # original session first to release any locks held by the failed
          # transaction.
          await sess.rollback()
          async with persistent.asession() as drop_sess:
              _cp_row = await drop_sess.get(persistent.ConversationPeer, _cp.id)
              if _cp_row is not None:
                  _cp_row.active = False
                  drop_sess.add(_cp_row)
              rcw_row = await drop_sess.get(persistent.ReadCapWAL, mw.bacap_stream)
              if rcw_row is not None:
                  rcw_row.next_index = rcr.next_message_box_index
                  rcw_row.substream_failure = _failure_reason(e)
                  drop_sess.add(rcw_row)
              mw_row = await drop_sess.get(persistent.MixWAL, mw.id)
              if mw_row is not None:
                  await drop_sess.delete(mw_row)
              await drop_sess.commit()
          substream_progress_queue.put_nowait(
              ("failed", str(mw.bacap_stream), _failure_reason(e)))
          give_up()
          return
      else:
          logger.error(
              "drain_mixwal_read_single: dropping unprocessable message on "
              "bacap_stream=%s: %s: %s; advancing past it",
              mw.bacap_stream, type(e).__name__, e,
          )
          await sess.rollback()
          async with persistent.asession() as drop_sess:
              rcw_row = await drop_sess.get(persistent.ReadCapWAL, mw.bacap_stream)
              if rcw_row is not None:
                  rcw_row.next_index = rcr.next_message_box_index
                  drop_sess.add(rcw_row)
              mw_row = await drop_sess.get(persistent.MixWAL, mw.id)
              if mw_row is not None:
                  await drop_sess.delete(mw_row)
              await drop_sess.commit()
          give_up()
          return

  if convlog_added:
    create_task(conversation_update_queue.put((notify_conv_id, False)))

  if tally_added:
    # A tally event was consumed and its transaction committed. The GUI lists
    # surveys from committed TallyState, so refreshing here is both safe and
    # race-free with the send-side commits (both go through SQLite).
    tally_update_queue.put_nowait(notify_conv_id)

  if peer_added:
    # Only announced once the transaction that added them has actually
    # committed (see _handle_introduction's docstring): firing this inside
    # the transaction would duplicate the notification on an
    # OperationalError retry that rolls the peer-add back and re-adds it.
    peer_added_queue.put_nowait(peer_added)
    readables_to_mixwal_event.set()

  if substream_progress:
    # Fire held substream events for the GUI Transfers panel,
    # after the commit so listeners never observe uncommitted pieces.
    for event in substream_progress:
      substream_progress_queue.put_nowait(event)

  if signal_send:
    # A tally sync request staged a reply on the outgoing stream; poke the
    # send loop now that the receive transaction has committed.
    await check_for_new()

  draining_right_now.discard(bacap_uuid)
  __resend_queue.discard(bacap_uuid)  # this should be .remove(), but why is it empty?
  readables_to_mixwal_event.set()  # signal readables_to_mixwal() so we can begin reading next
  __mixwal_updated.set()


async def pause_peer_reads(*, bacap_stream: uuid.UUID) -> None:
    """Pause polling without removing the peer from its conversation.

    Persist the pause before cancelling the reader so a concurrent sweep
    cannot restart it. Preserve received pieces and the next-index cursor.
    """
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
        if rcw is None:
            return
        peers = (await sess.exec(select(persistent.ConversationPeer).where(
            persistent.ConversationPeer.read_cap_id == bacap_stream,
        ))).all()
        if not any(peer.active for peer in peers):
            return
        rcw.paused = True
        sess.add(rcw)
        await sess.commit()
    task = _inflight_reads.get(bacap_stream)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Read failed while pausing %s", bacap_stream)
    _inflight_reads.pop(bacap_stream, None)
    __resend_queue.discard(bacap_stream)
    async with persistent.asession() as sess:
        mw_rows = (await sess.exec(select(persistent.MixWAL).where(
            persistent.MixWAL.bacap_stream == bacap_stream,
            persistent.MixWAL.is_read,
        ))).all()
        for mw in mw_rows:
            await sess.delete(mw)
        peers = (await sess.exec(select(persistent.ConversationPeer).where(
            persistent.ConversationPeer.read_cap_id == bacap_stream,
        ))).all()
        is_substream = any(
            peer.name.startswith(_SUBSTREAM_NAME_PREFIX) for peer in peers
        )
        await sess.commit()
    readables_to_mixwal_event.set()
    __mixwal_updated.set()
    if is_substream:
        substream_progress_queue.put_nowait(("paused", bacap_stream))


async def resume_peer_reads(*, bacap_stream: uuid.UUID) -> None:
    """Resume from the saved cursor, keeping already received pieces.

    An explicit retry also clears a terminal transfer failure and starts
    a fresh missing-box budget.
    """
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
        if rcw is None:
            return
        rcw.paused = False
        rcw.substream_missing_since = None
        rcw.substream_failure = None
        sess.add(rcw)
        peers = (await sess.exec(select(persistent.ConversationPeer).where(
            persistent.ConversationPeer.read_cap_id == bacap_stream,
        ))).all()
        for peer in peers:
            peer.active = True
            sess.add(peer)
        is_substream = any(
            peer.name.startswith(_SUBSTREAM_NAME_PREFIX) for peer in peers
        )
        await sess.commit()
    readables_to_mixwal_event.set()
    if is_substream:
        substream_progress_queue.put_nowait(("resumed", bacap_stream))


async def pause_upload(*, rcw_id: uuid.UUID) -> None:
    """Pause an outbound substream.

    Sets WriteCapWAL.paused, which keeps the chunk sweep and the write drain
    from casting its remaining C/F chunks, and cancels the in-flight write
    unless its envelope is already ACK'd. The pending MixWAL and PlaintextWAL
    rows stay in place so resume re-sends them idempotently. ``rcw_id`` is the
    indirection ReadCapWAL id (the Transfers row key).
    """
    agg = await _upload_stream_for_rcw(rcw_id)
    if agg is None:
        return
    async with persistent.asession() as sess:
        wcw = await sess.get(persistent.WriteCapWAL, agg)
        if wcw is not None:
            wcw.paused = True
            sess.add(wcw)
            await sess.commit()
    task = _inflight_writes.get(agg)
    if (
        task is not None and not task.done()
        and agg not in _write_acknowledged
    ):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The writer's cancellation is expected; only re-raise when this
            # caller is itself being cancelled.
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            logger.exception("write drain failed while pausing %s", agg)
    _inflight_writes.pop(agg, None)
    resendable_event.set()
    __mixwal_updated.set()
    substream_progress_queue.put_nowait(("upload_paused", rcw_id))


async def resume_upload(*, rcw_id: uuid.UUID) -> None:
    """Resume a paused outbound substream: clear its WriteCapWAL pause marker
    and poke the writer sweep to dispatch its next C/F chunk from the saved
    next_index. ``rcw_id`` is the indirection ReadCapWAL id."""
    agg = await _upload_stream_for_rcw(rcw_id)
    if agg is None:
        return
    async with persistent.asession() as sess:
        wcw = await sess.get(persistent.WriteCapWAL, agg)
        if wcw is not None:
            wcw.paused = False
            sess.add(wcw)
            await sess.commit()
    resendable_event.set()
    __mixwal_updated.set()
    substream_progress_queue.put_nowait(("upload_resumed", rcw_id))


async def cancel_upload(*, rcw_id: uuid.UUID) -> None:
    """Cancel an in-flight outbound substream before it is announced.

    Cancellable only while the substream is still draining: once every C/F
    chunk is ACK'd the gated I-chunk can dispatch, and there is nothing left to
    cancel. Stops the in-flight write, then in one transaction deletes the
    substream's MixWAL and PlaintextWAL rows, the I-chunk (and its MixWAL row),
    the indirection ReadCapWAL and the agg WriteCapWAL, and the optimistic
    ConversationLog bubble. Boxes already ACK'd are orphaned but unreachable:
    without the I-chunk no reader learns the substream's read cap.

    ``rcw_id`` is the indirection ReadCapWAL id (the Transfers row key).
    """
    async with persistent.asession() as sess:
        i_chunk = (await sess.exec(select(persistent.PlaintextWAL).where(
            persistent.PlaintextWAL.indirection == rcw_id,
        ))).first()
        if i_chunk is None:
            # The substream already completed and its I-chunk was dispatched
            # (or this cancel already ran): nothing to cancel.
            return
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        agg = rcw.write_cap_id if rcw is not None else None
        if agg is None:
            return
        i_chunk_id = i_chunk.id
        conv_id = i_chunk.conversation_id
    task = _inflight_writes.get(agg)
    if task is not None and not task.done():
        if agg in _write_acknowledged:
            # The envelope is ACK'd; let the ACK bookkeeping finish so the
            # outstanding-chunk count below is accurate.
            try:
                await task
            except Exception:
                logger.exception("write drain failed during cancel of %s", agg)
        else:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # The writer's cancellation is expected; only re-raise when
                # this caller is itself being cancelled.
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                logger.exception("write drain failed during cancel of %s", agg)
    _inflight_writes.pop(agg, None)
    __resend_queue.discard(agg)
    async with persistent.asession() as sess:
        # Re-check in the same transaction as the deletes: a chunk ACK that
        # landed while the write task was winding down can complete the
        # upload, which makes it uncancellable.
        i_chunk_row = await sess.get(persistent.PlaintextWAL, i_chunk_id)
        if i_chunk_row is None:
            return
        remaining = int((await sess.exec(
            select(persistent.sa.func.count())
            .select_from(persistent.PlaintextWAL)
            .where(persistent.PlaintextWAL.bacap_stream == agg)
        )).one())
        if remaining <= 0:
            return
        for mw in (await sess.exec(select(persistent.MixWAL).where(
            persistent.MixWAL.bacap_stream == agg,
        ))).all():
            await sess.delete(mw)
        # The I-chunk lives on the main stream, so its MixWAL row is keyed by
        # the PlaintextWAL id, not the agg stream.
        for mw in (await sess.exec(select(persistent.MixWAL).where(
            persistent.MixWAL.plaintextwal == i_chunk_id,
        ))).all():
            await sess.delete(mw)
        for pwal in (await sess.exec(select(persistent.PlaintextWAL).where(
            persistent.PlaintextWAL.bacap_stream == agg,
        ))).all():
            await sess.delete(pwal)
        # The bubble's outgoing_pwal FK points at the I-chunk, so it has to go
        # in the same transaction.
        convlog = (await sess.exec(select(persistent.ConversationLog).where(
            persistent.ConversationLog.outgoing_pwal == i_chunk_id,
        ))).first()
        if convlog is not None:
            await sess.delete(convlog)
        await sess.delete(i_chunk_row)
        rcw_row = await sess.get(persistent.ReadCapWAL, rcw_id)
        if rcw_row is not None:
            await sess.delete(rcw_row)
        wcw = await sess.get(persistent.WriteCapWAL, agg)
        if wcw is not None:
            await sess.delete(wcw)
        await sess.commit()
    __mixwal_updated.set()
    substream_progress_queue.put_nowait(("upload_cancelled", rcw_id))
    if conv_id is not None:
        await conversation_update_queue.put((conv_id, False))


async def _upload_stream_for_rcw(rcw_id: uuid.UUID) -> "uuid.UUID | None":
    """The agg_bacap_stream of an outbound substream, from its indirection
    ReadCapWAL id; None if the row is missing or is not an upload's.

    The main stream's own-peer ReadCapWAL also has a ``write_cap_id``, so the
    non-null ``substream_total_chunks`` (set only by
    ``SendOperation.serialize`` for an upload's indirection) is what
    discriminates the two.
    """
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, rcw_id)
        if rcw is None or rcw.write_cap_id is None:
            return None
        if rcw.substream_total_chunks is None:
            return None
        return rcw.write_cap_id


async def dismiss_failed_transfer(*, bacap_stream: uuid.UUID) -> None:
    async with persistent.asession() as sess:
        rcw = await sess.get(persistent.ReadCapWAL, bacap_stream)
        if rcw is None:
            return
        if rcw.substream_failure is None:
            # Already cleared, e.g. a resume landed between the row being
            # marked failed and the user dismissing it. Dismissal is
            # idempotent so the row never becomes unremovable.
            return
        peers = (await sess.exec(select(persistent.ConversationPeer).where(
            persistent.ConversationPeer.read_cap_id == bacap_stream,
        ))).all()
        if not peers or any(
            not peer.name.startswith(_SUBSTREAM_NAME_PREFIX) for peer in peers
        ):
            raise ValueError("Read cap is not a transfer")
        for peer in peers:
            parent = await _substream_parent(sess, peer.name)
            if parent is not None and rcw.read_cap is not None:
                await _discard_substream_release(
                    sess, parent.read_cap_id, rcw.read_cap,
                )
            peer.active = False
            sess.add(peer)
        for piece in (await sess.exec(select(persistent.ReceivedPiece).where(
            persistent.ReceivedPiece.read_cap == bacap_stream,
        ))).all():
            await sess.delete(piece)
        for row in (await sess.exec(select(persistent.MixWAL).where(
            persistent.MixWAL.bacap_stream == bacap_stream,
            persistent.MixWAL.is_read,
        ))).all():
            await sess.delete(row)
        rcw.substream_failure = None
        rcw.substream_missing_since = None
        rcw.paused = False
        sess.add(rcw)
        await sess.commit()


async def _wait_for_connection_or_shutdown(*, idle_retry_s: float = 0.0) -> bool:
    """Block until either __mixnet_connected is set or __should_quit fires.

    Returns True if the mixnet is reportedly connected, False if shutdown
    happened first. Loops use this at the top of each iteration so they
    pause cleanly during outages instead of burning cycles against a
    daemon that will only raise ThinClientOfflineError back at them.

    ``idle_retry_s`` bounds the wait: after it elapses the caller proceeds
    anyway (returning True) and relies on its own per-item error handling.
    Covers the case where ``__mixnet_connected`` is never re-set (a
    kpclientd restart can reconnect below the on_connection_status callback
    layer).
    """
    if __should_quit.is_set():
        return False
    if __mixnet_connected.is_set():
        return True
    waiters = (
        create_task(__mixnet_connected.wait()),
        create_task(__should_quit.wait()),
    )
    try:
        await asyncio.wait(
            waiters, timeout=(idle_retry_s or None),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
    return not __should_quit.is_set()


def _done_callback(task: "asyncio.Task", *, desc: str,
                   on_cancel=None, on_error=None) -> None:
    """Shared primitive for the fire-and-forget done-callbacks.

    The drain loops and on_error never await the tasks they fire, so a
    raised exception would otherwise be invisible (or, worse, only surface
    as asyncio's "Exception in callback" spam if a callback re-raises it
    -- same rationale as tests/test_katzen_util.py). Inspect the task
    directly instead: on cancellation call ``on_cancel()``; on an
    exception log ``desc`` with the exception and call ``on_error(exc)``;
    on success do neither. The exception is consumed (no "Task exception
    was never retrieved" warning) and NEVER re-raised.
    """
    def _done(task: "asyncio.Task") -> None:
        if task.cancelled():
            if on_cancel is not None:
                on_cancel()
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(f"{desc}: %r", exc, exc_info=exc)
        if on_error is not None:
            on_error(exc)
    if callable(getattr(task, "add_done_callback", None)):
        task.add_done_callback(_done)
    else:
        # A unit-test fake task object (only cancelled()/exception()): run
        # the inspection synchronously so direct calls like
        # `_on_write_done(fake_task, ...)` keep working.
        _done(task)


def _on_write_done(task: "asyncio.Task", stream: uuid.UUID,
                   draining_right_now):
    """Done-callback for the fire-and-forget write drains in drain_mixwal2.

    Discards the stream from draining_right_now so a crashed write does not
    starve every later MW on it (the MixWAL row survives for the next pass),
    logs the failure, and pokes __mixwal_updated so the retry is prompt.
    """
    _inflight_writes.pop(stream, None)
    _write_acknowledged.discard(stream)
    _done_callback(
        task,
        desc=(
            f"drain_mixwal_write_single crashed for bacap_stream={stream}; "
            "releasing stream for another drain pass"
        ),
        on_cancel=lambda: (draining_right_now.discard(stream), None),
        on_error=lambda _exc: (
            draining_right_now.discard(stream), __mixwal_updated.set(),
        ),
    )


async def drain_mixwal2(connection: ThinClient) -> None:
    """Read from MixWAL and put the messages on the network."""
    """"Send messages to mixnet from MixWAL.

    - Listen for new entries in MixWAL
    - for each envelope_hash:
      - if we don't have a resend state for it, create it
      - ThinClient.send_message
      - start timer that resends it
      - if a reply from courier comes in:
        - delete timer
        - delete from MixWAL
        - bump MessageBoxIndex
    """

    draining_right_now : "Set[uuid.UUID]" = set()
    await __resend_queue_populated.wait()
    shutdown = create_task(__should_quit.wait())
    requests: set[asyncio.Task[None]] = set()
    try:
        while not __should_quit.is_set():
            # asyncio.wait defaults to ALL_COMPLETED, which would force this
            # loop to wait the full timeout (or for shutdown) regardless of
            # __mixwal_updated being set, effectively turning it into a
            # 15-second poller. FIRST_COMPLETED restores the event-driven
            # behaviour the caller of __mixwal_updated.set() expects.
            updated = asyncio.create_task(__mixwal_updated.wait())
            try:
                await asyncio.wait(
                    (updated, shutdown), timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                await _cancel_and_join((updated,))
            if __should_quit.is_set():
              continue
            __mixwal_updated.clear()
            # Read fresh, after the wait: a reconnect that happens *during* the
            # wait (on_connection_status also pokes __mixwal_updated on one, so
            # this pass runs promptly) must not be judged by a connectedness
            # snapshot taken up to 15s earlier, or a pending write sits deferred
            # for a further sweep instead of going out immediately.
            connected = __mixnet_connected.is_set()
            logger.debug("DRAIN_MIXWAL draining_right_now:%s __resend_queue:%s", draining_right_now, __resend_queue)
            # TODO drain new from mixwal, this should NOT be a long-running session like it currently is
            new_write_mws = []
            async with persistent.asession() as sess:
                new_mixwals = (await sess.exec(persistent.MixWAL.get_new(draining_right_now))).all()
                for mw in new_mixwals:
                    if mw.is_read:
                        # Reads are cast even while on_connection_status reports
                        # the daemon offline: kpclientd's own ARQ holds the
                        # request and rides out gateway-link flaps, delivering
                        # when the link recovers. Gating reads on
                        # __mixnet_connected strands them forever if the
                        # re-enabled status notification is ever lost.
                        draining_right_now.add(mw.bacap_stream)
                        __resend_queue.add(mw.bacap_stream)
                        rcw = await sess.get(persistent.ReadCapWAL, mw.bacap_stream)
                        if rcw is None:
                            draining_right_now.discard(mw.bacap_stream)
                            __resend_queue.discard(mw.bacap_stream)
                            continue
                        if rcw.paused:
                            draining_right_now.discard(mw.bacap_stream)
                            __resend_queue.discard(mw.bacap_stream)
                            continue
                        if len(rcw.read_cap) != 136:
                            # A malformed row must not take down the whole drain
                            # loop (see drain_mixwal's wrapper, which catches an
                            # escaped exception here but then simply returns,
                            # permanently ending every read AND write drain for
                            # the rest of the process). Skip only this stream,
                            # the same log-and-continue contract every other
                            # per-item failure path in this loop already gets.
                            logger.error(
                                "drain_mixwal: ReadCapWAL.read_cap for "
                                "bacap_stream=%s has incorrect length %d "
                                "(expected 136); skipping this stream: %r",
                                mw.bacap_stream, len(rcw.read_cap), rcw,
                            )
                            draining_right_now.discard(mw.bacap_stream)
                            __resend_queue.discard(mw.bacap_stream)
                            continue
                        read_task = create_task(drain_mixwal_read_single(connection=connection, rcw_read_cap=rcw.read_cap, mw=mw, draining_right_now=draining_right_now))
                        requests.add(read_task)
                        read_task.add_done_callback(requests.discard)
                        _inflight_reads[mw.bacap_stream] = read_task

                        def _on_read_done(task, stream=mw.bacap_stream) -> None:
                            _inflight_reads.pop(stream, None)
                            # The drain loop never awaits read_task, so without
                            # this an unhandled exception (e.g. an OS-level send
                            # failure mid-bounce) would strand the stream in
                            # draining_right_now forever, silently starving
                            # every later box on it. give_up() already discards
                            # on the handled paths; discard is idempotent.
                            _done_callback(
                                task,
                                desc=(
                                    f"drain_mixwal_read_single crashed for "
                                    f"bacap_stream={stream}; releasing stream "
                                    "for another drain pass"
                                ),
                                on_cancel=(
                                    lambda: (draining_right_now.discard(stream), None)
                                ),
                                on_error=lambda _exc: (
                                    draining_right_now.discard(stream), None
                                ),
                            )
                            readables_to_mixwal_event.set()

                        read_task.add_done_callback(_on_read_done)
                    elif connected:
                        wcw = await sess.get(
                            persistent.WriteCapWAL, mw.bacap_stream,
                        )
                        if wcw is None or wcw.paused:
                            # A paused stream keeps its MixWAL row; resume
                            # re-sends it.
                            continue
                        new_write_mws.append(mw)
                    else:
                        # Defer the write dispatch until the daemon reports
                        # connected again; the daemon-side ARQ ride-out for
                        # writes depends on the gate (see test_client_reconnect).
                        logger.debug("drain_mixwal: deferring (write) MIXWAL is_read=%s bacap_stream=%s until connected", mw.is_read, mw.bacap_stream)
            for mw in new_write_mws:
                logger.debug("drain_mixwal: NEW (write) MIXWAL is_read=%s bacap_stream=%s",
                             mw.is_read, mw.bacap_stream)
                draining_right_now.add(mw.bacap_stream) # this is the uuid PK
                __resend_queue.add(mw.bacap_stream)  # ensure readables_to_mixwal() does not serialize new ones for this stream

                write_task = create_task(drain_mixwal_write_single(connection, mw, draining_right_now))
                # Registered so pause and cancel can stop this chunk.
                _inflight_writes[mw.bacap_stream] = write_task
                requests.add(write_task)
                write_task.add_done_callback(requests.discard)
                write_task.add_done_callback(
                    lambda task, b=mw.bacap_stream: _on_write_done(task, b, draining_right_now))

    finally:
        await _cancel_and_join((*requests, shutdown))


async def provision_read_caps(connection: ThinClient):
    """Long-running process to tread persistent.WriteCapWAL and populate ReadCapWAL"""
    #print("provision read caps"*100)
    import sqlalchemy as sa
    wait = 0
    while not __should_quit.is_set():
        await asyncio.sleep(wait)  # could make this smoother with an asyncio.Event(), but 5s is fine for now.
        wait = 5
        async with persistent.asession() as sess:
            for (rcw, wcw) in await sess.exec(sa.select(persistent.ReadCapWAL,persistent.WriteCapWAL).where(persistent.ReadCapWAL.read_cap == None).where(persistent.ReadCapWAL.write_cap_id==persistent.WriteCapWAL.id)): #  &
                logger.debug("provision_read_caps UPDATING rcw_id=%s wcw_id=%s",
                             rcw.id, wcw.id)
                if wcw.write_cap is None:
                    try:
                        keypair_res = await _rpc_racing_connection_life(
                            bacap_uuid=wcw.id,
                            what="new_keypair",
                            rpc_factory=lambda: connection.new_keypair(
                                seed=secrets.token_bytes(32),
                            ),
                            backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception as e:
                        logger.warning(
                            "new_keypair did not work: %s", e,
                            exc_info=not _retryable_rpc_error(e),
                        )
                        continue
                    wcw.write_cap = keypair_res.write_cap
                    wcw.next_index = keypair_res.first_message_index
                    rcw.read_cap = keypair_res.read_cap
                    rcw.next_index = keypair_res.first_message_index
                    sess.add(wcw)
                    sess.add(rcw)
                    await sess.commit()
                    resendable_event.set() # start sending PlaintextWAL msgs that were waiting on this WriteCap
                    readables_to_mixwal_event.set() # start reading the ReadCap if it's active
                    continue
                else:
                    logger.warning("DB was created with old API; new API does not support converting write cap to read cap")
                    continue
            await sess.commit()

async def readables_to_mixwal(connection: ThinClient) -> None:
    """
    Look up all of our read caps, start sending reads for all the "active" ones that we
    aren't currently trying to read.
    """
    logger.debug("readables_to_mixwal: starting")
    global __resend_queue
    await __resend_queue_populated.wait()
    async def process_box(cpeer:persistent.ConversationPeer, rcw:persistent.ReadCapWAL) -> persistent.MixWAL:
        rcreply = await asyncio.wait_for(
            connection.encrypt_read(read_cap=rcw.read_cap, message_box_index=rcw.next_index),
            timeout=_DAEMON_RPC_TIMEOUT_SECONDS,
        )
        logger.debug("process_box got this from encrypt_read: %s", rcreply)
        mw = persistent.MixWAL(
            bacap_stream=rcw.id,
            plaintextwal=None,
            envelope_hash=rcreply.envelope_hash,
            encrypted_payload=rcreply.message_ciphertext,
            envelope_descriptor=rcreply.envelope_descriptor,
            next_message_index=rcreply.next_message_box_index,
            current_message_index=rcw.next_index,
            # rcreply.reply_index
            is_read=True,
        )
        return mw
    while not __should_quit.is_set():
        # Pause while the mixnet is unreachable so the loop does not
        # try to encrypt_read against a daemon that cannot route. The
        # wait is bounded by _CONNECTION_IDLE_RETRY_S so a kpclientd
        # restart (which can leave the latch cleared) does not strand
        # the read-arming loop, the only source of is_read MixWAL rows.
        if not await _wait_for_connection_or_shutdown(idle_retry_s=_CONNECTION_IDLE_RETRY_S):
            continue
        logger.debug("SLEEPING FOR READABLES_TO_MIXWAL"*2)
        try:
            await asyncio.wait_for(readables_to_mixwal_event.wait(), timeout=_ARMING_SWEEP_S)
        except TimeoutError:
            pass
        if __should_quit.is_set():
            continue
        readables_to_mixwal_event.clear()
        # Run the pass whether the event fired or the _ARMING_SWEEP_S timeout
        # elapsed: the fixed cadence re-arms streams even while the daemon
        # is down and nothing pokes the event.
        logger.debug("IN READABLES_TO_MIXWAL_LOOP")
        retry_needed = False
        try:
            async with persistent.asession() as sess:
                # TODO are these guaranteed to be distinct?
                readable_peers = (await sess.exec(select(
                    persistent.ConversationPeer, persistent.ReadCapWAL
                ).where(persistent.ReadCapWAL.paused == False
                        ).where(persistent.ConversationPeer.active==True
                        ).where(persistent.ConversationPeer.read_cap_id == persistent.ReadCapWAL.id
                                ).where(
                                    persistent.ReadCapWAL.id.not_in(select(persistent.MixWAL.bacap_stream)) # todo does the rcw.id correspond to a bacap_stream?? should we use the same id for both?
                                )
                    )
                ).all()
                logger.debug("readable_peers: %d", len(readable_peers))
                # Two ConversationPeer rows can transiently alias the same
                # ReadCapWAL (e.g. a stale duplicate self-peer). bacap_stream
                # is MixWAL's primary key, so an arming pass that adds the
                # same stream twice raises IntegrityError on its single
                # commit (UNIQUE constraint failed: mixwal.bacap_stream);
                # arm each read cap at most once per pass.
                armed: set[uuid.UUID] = set()
                for (cpeer, rcw) in readable_peers:
                    if rcw.id in armed:
                        logger.warning(
                            "readables_to_mixwal: skipping duplicate rcw=%s (%s)",
                            rcw.id, cpeer.name,
                        )
                        continue
                    armed.add(rcw.id)
                    logger.debug("going to process_box", cpeer.name, rcw.next_index[:8].hex())
                    try:
                      mw = await process_box(cpeer, rcw)
                    except Exception as e:
                      logger.warning(
                          "Read setup failed; retrying: %s", e,
                          exc_info=not _retryable_rpc_error(e),
                      )
                      retry_needed = True
                      continue
                    sess.add(mw)
                    logger.debug("finished one peer: %s", cpeer.name)
                logger.debug("readables_to_mixwal: committing")
                await sess.commit()
        except (IntegrityError, OperationalError) as e:
            # Retry lock contention and a duplicate arming without publishing
            # a failed pass. Other database errors keep their traceback.
            if not _is_transient_sqlite_busy(e) and not _is_duplicate_arming(e):
                raise
            logger.warning(
                "readables_to_mixwal: retrying next sweep: %s", e,
            )
            await asyncio.sleep(5)
            readables_to_mixwal_event.set()
            continue
        logger.debug("done readables_to_mixwal: %d peers", len(readable_peers))
        if len(readable_peers):
            __mixwal_updated.set()
            logger.debug("__mixwal_updated.set() from readables_to_mixwal")
        if retry_needed:
            await asyncio.sleep(5)
            readables_to_mixwal_event.set()


async def readables_to_mixwal_supervised(connection: ThinClient) -> None:
    """Keep the read-arming loop alive across an unexpected pass failure.

    ``readables_to_mixwal`` is the session's only source of is_read MixWAL
    rows, and it deliberately re-raises invariant errors (a transient sqlite
    lock is handled inside it) rather than swallow them. Without this wrapper
    a single unexpected error would end the task and wedge every read for the
    rest of the session, so log the traceback loudly and restart the loop.
    The bounded wait keeps a persistently failing pass from spinning.
    """
    await _supervised(
        readables_to_mixwal, connection, on_restart=readables_to_mixwal_event.set,
    )


# A peer announces substreams with I-chunks, and each unresolved one buys a
# full missing-box budget of mixnet reads before it retires. Cap how many a
# single parent peer can have open at once so a hostile announcer cannot
# multiply that cost without bound.
_MAX_OPEN_SUBSTREAMS_PER_PEER = 8
_OVER_CAP_FAILURE = "Too many transfers at once from this peer"

_SUPERVISOR_RETRY_S = 5.0
_SUPERVISOR_RETRY_MAX_S = 60.0
_SUPERVISOR_HEALTHY_S = 300.0


async def _supervised(worker, connection: ThinClient, *, on_restart=None) -> None:
    """Run ``worker`` forever, restarting it after a failure or an early
    return, paced so a deterministic failure cannot spin.

    The pause is unconditional. _wait_for_connection_or_shutdown returns at
    once while the daemon is connected, so it paces nothing on its own: a
    non-transient error (a malformed database, a full disk) would otherwise
    restart at the speed the error returns, logging a traceback each time.
    """
    delay = _SUPERVISOR_RETRY_S
    while not __should_quit.is_set():
        started = time.monotonic()
        try:
            await worker(connection)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.critical(
                "%s died; restarting it", getattr(worker, "__name__", worker),
                exc_info=True,
            )
        else:
            if __should_quit.is_set():
                return
            logger.critical(
                "%s returned early; restarting it",
                getattr(worker, "__name__", worker),
            )
        # A worker that ran healthily for a long stretch before dying is not
        # in a failure loop, so start its next backoff from the floor rather
        # than keeping a ratchet from hours ago.
        if time.monotonic() - started >= _SUPERVISOR_HEALTHY_S:
            delay = _SUPERVISOR_RETRY_S
        await asyncio.sleep(delay)
        delay = min(_SUPERVISOR_RETRY_MAX_S, delay * 2.0)
        if await _wait_for_connection_or_shutdown(
            idle_retry_s=_CONNECTION_IDLE_RETRY_S,
        ) and on_restart is not None:
            on_restart()


def on_error(task, func, *args, **kwargs):
    """Attach ``func(*args, **kwargs)`` to ``task``'s completion, firing only
    when the task raised.

    The exception is consumed (no "Task exception was never retrieved"
    warning) but NOT re-raised: a done-callback's raise can only be observed
    by asyncio's exception handler, which logs a spurious "Exception in
    callback" traceback for transient link drops (e.g. a resendable
    plaintext hitting the dead link during a kpclientd bounce). The caller
    reschedules the work on its next sweep.
    """
    _done_callback(
        task,
        desc="on_error: task failed",
        on_error=lambda exc, f=func, a=args, k=kwargs: f(*a, **k),
    )
    return task

async def send_resendable_plaintexts(connection:ThinClient) -> None:
    # look at persistent.PlaintextWAL:
    # PICK OUT stuff in PlaintextWAL that we aren't currently resending
    # - mark them as "being resent" (in memory)
    # - when we get an ACK, we move it from being resent to "sent" (db),
    #   and remove it from the PlaintextWAL, atomically
    global __resend_queue
    __resend_queue |= await persistent.MixWAL.resend_queue_from_disk()
    __resend_queue_populated.set()
    requests: set[asyncio.Task[None]] = set()
    try:
        while not __should_quit.is_set():
            # Pause while the mixnet is unreachable; encrypt_write/
            # start_resending need a live route through kpclientd. Bounded by
            # _CONNECTION_IDLE_RETRY_S the same way as readables_to_mixwal's
            # gate.
            if not await _wait_for_connection_or_shutdown(idle_retry_s=_CONNECTION_IDLE_RETRY_S):
                continue
            try:
                await asyncio.wait_for(resendable_event.wait(), timeout=_ARMING_SWEEP_S)
            except TimeoutError:
                pass
            if __should_quit.is_set():
                continue
            resendable_event.clear()
            logger.debug("send_resendable_plaintexts: running")
            pwals_to_send = set()
            async with persistent.asession() as sess:
                query = persistent.PlaintextWAL.find_resendable(__resend_queue)
                sendable_rows = (await sess.exec(query)).all()
                # find_resendable wraps PlaintextWAL inside a row_number()
                # subquery and returns plain Row tuples that are not
                # ORM-tracked. For an indirection PWAL we re-fetch the
                # tracked instance to fill in its bacap_payload, then
                # snapshot the (id, bacap_stream, payload) we need for
                # dispatch into a detachable container so start_resending
                # never touches a session-bound attribute.
                dispatch: "list[types.SimpleNamespace]" = []
                for row in sendable_rows:
                    payload = row.bacap_payload
                    if row.indirection is not None and not payload:
                        # find_resendable only surfaces indirection PWALs
                        # whose target ReadCapWAL has been provisioned
                        # (read_cap IS NOT NULL), so the get is guaranteed
                        # to find a populated read_cap here.
                        logger.debug(
                            "filling indirection PWAL %s from rcw %s",
                            row.id, row.indirection,
                        )
                        rcw = await sess.get(persistent.ReadCapWAL, row.indirection)
                        # Extended I-chunk carries the total plaintext
                        # chunk count (C-chunks + final F) as a 4-byte big-endian
                        # prefix, so the reader can render progress n/total. rcw is
                        # the sender's indirection ReadCapWAL created in
                        # models.serialize(); when it predates the column this
                        # falls back to a legacy 136-byte I-chunk.
                        total = rcw.substream_total_chunks if rcw is not None else None
                        if total is None:
                            payload = b'I' + rcw.read_cap
                        else:
                            payload = b'I' + struct.pack(">I", total) + rcw.read_cap
                        pwal_orm = await sess.get(persistent.PlaintextWAL, row.id)
                        if pwal_orm is not None:
                            pwal_orm.bacap_payload = payload
                            sess.add(pwal_orm)
                    dispatch.append(types.SimpleNamespace(
                        id=row.id,
                        bacap_stream=row.bacap_stream,
                        bacap_payload=payload,
                    ))
                try:
                    await sess.commit()
                except OperationalError as e:
                    if not _is_transient_sqlite_busy(e):
                        raise
                    logger.warning(
                        "send_resendable_plaintexts: sqlite busy; retrying on the next sweep: %s", e,
                    )
                    continue
            for pwal in dispatch:
                if pwal.bacap_stream not in __resend_queue:
                    __resend_queue.add(pwal.bacap_stream)
                    t = create_task(start_resending(connection, pwal))
                    requests.add(t)
                    t.add_done_callback(requests.discard)
                    # Default-arg capture pins pwal.bacap_stream at lambda
                    # creation time. The prior `lambda: ... pwal.bacap_stream`
                    # closed over the loop variable and, on an inner iteration's
                    # failure, discarded the LAST iteration's bacap_stream,
                    # stranding the actual failer in __resend_queue forever.
                    on_error(t, lambda s=pwal.bacap_stream: __resend_queue.discard(s))  # when cancelled/exception

    finally:
        await _cancel_and_join(requests)

async def start_resending(connection:ThinClient, pwal: persistent.PlaintextWAL):
    """
    called by network:send_resumable_plaintexts, at startup and peridically, guarded by __resend_queue
    creates MixWAL entries for plaintexts.

    Given a PlaintextWAL entry (plaintext bacap_payload, bacap_stream uuid)
    we need to call:
    - create_write_channel() ->     alice_channel_id, read_cap, write_cap = await alice_thin_client.create_write_channel()
      - persist to bacap_stream_uuid -> read_cap/write_cap
        - what do we do about next_index? we can recover it from the write_cap
        - that lets us call resume_write_channel(write_cap, message_box_index=write_cap[FOO:])
    - write_reply = await alice_thin_client.write_channel(alice_channel_id, pwal.bacap_payload)
      - this give us a WriteChannelReply containing:
        send_message_payload
        current_message_index
        next_message_index
        envelope_descriptor
        envelope_hash
      - these we persist to MixWAL
    """
    async with persistent.asession() as sess:
        wc: persistent.WriteCapWAL = await sess.get(persistent.WriteCapWAL, pwal.bacap_stream)

    # now we have:
    # - a plaintext to send to send, pwal.bacap_payload
    # - wc has .write_cap, .next_index
    # and we need to:
    # - pick a courier
    # - encrypt the message
    # - persist that to MixWAL

    wcr : "EncryptWriteResult" = await _rpc_racing_connection_life(
        bacap_uuid=pwal.bacap_stream,
        what="encrypt_write",
        rpc_factory=lambda: connection.encrypt_write(
            write_cap=wc.write_cap,
            message_box_index=wc.next_index,
            plaintext=pwal.bacap_payload,
        ),
        backstop_s=_DAEMON_RPC_TIMEOUT_SECONDS,
    )

    next_message_index = wcr.next_message_box_index

    mw = persistent.MixWAL(
        bacap_stream=pwal.bacap_stream,
        plaintextwal=pwal.id,
        envelope_hash = wcr.envelope_hash,
        encrypted_payload = wcr.message_ciphertext,
        envelope_descriptor = wcr.envelope_descriptor,
        current_message_index = wc.next_index,
        next_message_index = next_message_index, # for resends, do we need current_message_index ?
        is_read = False
    )
    async with persistent.asession() as sess:
        sess.add(mw)
        # TODO if we do this, how do we know what to remove from PlaintextWAL?
        # we need to hook network.on_message_reply to look for the returns
        await sess.commit()
    # Now we have persisted our intention to resend.
    # Next up is something needs to actually resend, reading from MixWAL
    # and issuing ThinClient.start_resending_encrypted_message
    __mixwal_updated.set()

async def on_daemon_disconnected(event):
    await on_connection_status({"is_connected": False})


async def on_connection_status(status:"Dict[str,Any]"):
    global _last_connected, _reconnect_event
    connected = bool(status["is_connected"])
    transitioned = _last_connected is not None and _last_connected != connected
    err = status.get("err") or status.get("Err")
    if connected:
        __mixnet_connected.set()
        if transitioned:
            logger.info("daemon reports reconnected to mixnet")
            # Replace (not just re-set) the event: a read that started
            # waiting before this transition sees ITS captured instance
            # fire; a read that starts waiting after this point captures the
            # new instance and correctly waits for the NEXT reconnect only.
            old_event, _reconnect_event = _reconnect_event, asyncio.Event()
            old_event.set()
            __mixwal_updated.set()  # a deferred write need not wait out the next sweep
    else:
        __mixnet_connected.clear()
        if (transitioned or _last_connected is None) and not err:
            # Warn only on the transition (or the first report ever), not on
            # every failed reconnect attempt the daemon retries in the
            # background: those repeat every ~15-30s during an outage and
            # would otherwise log this line just as often. A disconnect that
            # also carries an err payload is left to the ERROR log below
            # instead of logging the same single event twice.
            logger.warning("daemon reports disconnected from mixnet; ARQ rides out and retries")
    _last_connected = connected
    if err:
        logger.error("ON_CONNECTION_STATUS err: %s", status)
        #ON_CONNECTION_STATUS err: {'is_connected': False, 'err': {'Op': 'read', 'Net': 'tcp', 'Source': {'IP': b'\x7f\x00\x00\x01', 'Port': 51718, 'Zone': ''}, 'Addr': {'IP': b'\x7f\x00\x00\x01', 'Port': 30004, 'Zone': ''}, 'Err': {}}}
        # why is clientd telling us about the IP addresses its trying to connect to?
        # and why does it have both status['err'] and status['Err']?
        #import pdb;pdb.set_trace()
        return

async def on_message_reply(reply):
    """Gets called each time a message reply comes in, whether it's from
    something we wrote or read.
    TODO pretty annoying that it's not async ...
    """
    # Receives something like:
    # {'message_id': b'\n\x90\xc2\x0cr\xa8\xa2+\x17Y\xcb\x837\xcc\x0f\x9b', 'surbid': None, 'payload': None}
    if async_queue := __on_message_queues.get(reply['message_id'], None):
        logger.debug("on_message_reply: matched queue for message_id=%s payload=%s",
                     reply['message_id'].hex(), reply['payload'])
        create_task(async_queue.put(reply))
        logger.debug("on_message_reply: enqueued message_id=%s", reply['message_id'].hex())
    else:
        logger.debug("on_message_reply: no queue match, reply=%s", reply)
    return
    # TODO wait for ACK, then:
    # once reply comes in:
    """
    async with persistent.asession() as sess:
        #   listener should put uuid in SentLog
        sess.add(persistent.SentLog(pwal.id))
        #   listener should remove from PlaintextWAL
        sess.delete(pwal)
        #   resend envelope deleted from MixWAL:
        sess.delete(mw)
        await sess.commit()
    """

async def on_message_sent(reply):
    """Example:
    {'message_id': b'\xe1\xb85\xe8u]\xf8\x85\xa9\xa7\xac\xf7\xcc\xe6\xdfQ',
    'surbid': b'\xf3\xa1\xfdni\r2\xe9\xbalH\xcfK\x89\x8e\xee',
    'sent_at': 1751741438,
    'reply_eta': 0,  # TODO we should make the resend try to match the reply_eta
    'err': 'client/conn: PKI error: client2: failed to find destination service node: pki: service not found'}
    """
    if err := reply.get('err', None):
        logger.error("ERR for outgoing message_id=%s: %s", reply['message_id'].hex(), err)
    else:
        logger.debug("MESSAGE SENT OK: message_id=%s reply=%s", reply['message_id'].hex(), reply)

def resolve_thinclient_config(explicit: "str | Path | None" = None) -> Path:
    """Locate ``thinclient.toml`` using a precedence chain.

    The chain is consulted in order, returning the first path that
    exists on disk:

      1. ``explicit`` argument (raises ``FileNotFoundError`` if given
         and missing; the caller asked for this file specifically),
      2. ``$KATZENQT_THINCLIENT_CONFIG``,
      3. ``$XDG_CONFIG_HOME/katzenqt/thinclient.toml`` (default
         ``~/.config/katzenqt/thinclient.toml``),
      4. the bundled copy shipped under ``katzenqt/data/thinclient.toml``
         (resolved via ``importlib.resources``),
      5. the development-tree fallback at
         ``<repo>/config/thinclient.toml``.
    """
    if explicit is not None:
        explicit_path = Path(explicit)
        if not explicit_path.is_file():
            raise FileNotFoundError(
                f"thinclient config not found at explicit path: {explicit_path}"
            )
        return explicit_path

    candidates: "list[Path]" = []
    env = os.environ.get("KATZENQT_THINCLIENT_CONFIG")
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates.append(Path(xdg) / "katzenqt" / "thinclient.toml")
    try:
        bundled = importlib.resources.files("katzenqt") / "data" / "thinclient.toml"
        candidates.append(Path(str(bundled)))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    candidates.append(
        Path(__file__).resolve().parent.parent.parent / "config" / "thinclient.toml"
    )

    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Could not locate thinclient.toml in: "
        + ", ".join(str(c) for c in candidates)
    )


# from katzenpost_thinclient import ThinClient, Config
async def reconnect(config_path: "str | Path | None" = None) -> ThinClient:
    resolved = resolve_thinclient_config(config_path)
    cfg = ThinClientConfig(
        str(resolved),
        on_message_reply=on_message_reply,
        on_message_sent=on_message_sent,
        on_connection_status=on_connection_status,
        on_daemon_disconnected=on_daemon_disconnected,
        on_new_pki_document=on_new_pki_document,
    )
    client = ThinClient(cfg)
    await client.start(asyncio.get_running_loop())  # this can throw exceptions
    return client

# events: we should keep track of ConnectionStatusEvent.IsConnected so we can tell the user whether the mixnet client is working

# ThinClient.send_message(surb_id, payload, dest_node, dest_queue)

def create_new_keypair(seed: bytes):
    """Makes a new WriteCap/ReadCap pair from a 32byte seed, using blake2b as KDF"""
    assert len(seed) == 32
    assert isinstance(seed, bytes)
    from nacl.hash import blake2b
    from nacl.signing import SigningKey
    def gen_bytes(purpose:bytes, length:int) -> bytes:
        return blake2b(
            data=b'KP:'+purpose,
            key=seed, # IKM
            salt=b'',
            person=b'', digest_size=length, encoder=nacl.encoding.RawEncoder
        )
    start_idx_raw:bytes = gen_bytes(b'start_idx', 16)
    idx1, idx2 = struct.unpack('<2Q', start_idx_raw)
    start_idx = struct.pack('<Q', (idx1 + idx2) & 0x7fffffffffffffff)
    priv_obj = SigningKey(gen_bytes(b'signing_key', 32))
    first_message_index = start_idx + gen_bytes(b'blinding_factor', 32) + gen_bytes(b'encryption_key', 32) + gen_bytes(b'HKDF_state',32)
    assert len(first_message_index) == 104 # 8 + 32 + 32 + 32
    read_cap = priv_obj.verify_key.encode() + first_message_index
    write_cap = priv_obj.encode() + read_cap
    assert write_cap[32:] == read_cap[:]
    assert write_cap[:32] != read_cap[:32]
    assert len(write_cap) == 32 + 32 + 104
    assert len(read_cap)  == 32 + 104
    return write_cap, read_cap

async def test_keypair(connection, write_cap, read_cap):
    """Test that create_new_keypair() results in usable+matching write/read caps."""
    wcr = await connection.encrypt_write(
        plaintext=b'hello',
        write_cap=write_cap,
        message_box_index=write_cap[-104:])
    await connection.start_resending_encrypted_message(
        read_cap=None, write_cap=write_cap, message_box_index=None,
        reply_index=None,
        envelope_descriptor=wcr.envelope_descriptor,
        message_ciphertext=wcr.message_ciphertext,
        envelope_hash=wcr.envelope_hash)

    await asyncio.sleep(20)

    rcr = await connection.encrypt_read(
        read_cap=read_cap,
        message_box_index=read_cap[-104:])
    await connection.start_resending_encrypted_message(
        read_cap=read_cap, write_cap=None,
        message_box_index=read_cap[-104:],
        reply_index=None,
        envelope_descriptor=rcr.envelope_descriptor,
        message_ciphertext=rcr.message_ciphertext,
        envelope_hash=rcr.envelope_hash)
