"""`FakeThinClient`: an in-process stand-in for `katzenpost_thinclient.ThinClient`.

The fake satisfies just enough of the ThinClient method surface to drive
`katzenqt.network` through its branches without involving a real socket,
the real daemon, the mixnet, the PKI, or any cryptography. Because all
encryption and Sphinx work happens inside `kpclientd` in production, and
both `thin_client` and `network.py` only marshal CBOR blobs around it,
the fake's payloads can be opaque tokens that the fake remembers by
`envelope_hash` and replays on `start_resending_encrypted_message`.

The bookkeeping pivots on the BACAP keypair invariant that
`write_cap[32:] == read_cap` (see `katzenqt.network.create_new_keypair`),
which lets the fake's "box store" be keyed by the read_cap bytes
regardless of whether the caller arrived via the write side or the read
side.

Per-test injection knobs (see the attribute names below) let a single
fake cover both happy paths and pathological ones: timeouts, daemon
hiccups, courier-disappeared-from-PKI mid-flight, decryption failures,
and unanswered ACKs that model a stuck send.
"""
from __future__ import annotations

import asyncio
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from katzenpost_thinclient import (
    BACAPDecryptionFailedError,
    StartResendingCancelledError,
    ThinClientOfflineError,
)
from katzenpost_thinclient.core import (
    BoxIDNotFoundError,
    MKEMDecryptionFailedError,
)
from katzenpost_thinclient.pigeonhole import (
    EncryptReadResult,
    EncryptWriteResult,
    KeypairResult,
    StartResendingResult,
)

from katzenqt.network import create_new_keypair


@dataclass
class FakeServiceDescriptor:
    """Stand-in for `katzenpost_thinclient.ServiceDescriptor`.

    Only ``to_destination()`` is consumed by `network.py`, so that is all
    we model.
    """
    identity_hash: bytes
    queue_id: bytes

    def to_destination(self) -> Tuple[bytes, bytes]:
        return (self.identity_hash, self.queue_id)


@dataclass
class _Envelope:
    """Bookkeeping for a single encrypt_{read,write} reply.

    On `start_resending_encrypted_message` the fake looks up the
    corresponding entry by `envelope_hash` and applies its read-or-write
    effect against the box store.
    """
    is_write: bool
    box_id: bytes
    message_box_index: bytes
    next_message_box_index: bytes
    plaintext: bytes  # only meaningful for writes
    cap: bytes        # the write_cap or read_cap presented at encrypt time


class FakeThinClient:
    """Drop-in for `katzenpost_thinclient.ThinClient` covering only the
    methods `network.py` calls.

    Construction does not require any configuration; tests can mutate the
    public attributes (``box_store``, ``next_errors``, ``pending_acks``,
    ``couriers``) to steer behaviour.
    """

    def __init__(self) -> None:
        # envelope_hash -> the request that produced it
        self.envelopes: Dict[bytes, _Envelope] = {}
        # (box_id, message_box_index) -> plaintext
        self.box_store: Dict[Tuple[bytes, bytes], bytes] = {}
        # every keypair issued via new_keypair()
        self.keypairs: List[KeypairResult] = []
        # ordered record of every method call the test can inspect
        self.call_log: List[Tuple[str, Dict[str, Any]]] = []
        # per-method queues of exceptions to pop on the next call
        self.next_errors: Dict[str, List[Exception]] = {}
        # envelope_hashes whose start_resending hangs forever
        self.pending_acks: set[bytes] = set()
        # advertised couriers; tests may add/remove mid-flight
        self.couriers: List[Tuple[bytes, bytes]] = [
            (secrets.token_bytes(32), b"+courier"),
        ]
        # cached PKI document handed out by pki_document()
        self._pki_doc: Dict[str, Any] = {"ServiceNodes": [], "Epoch": 0}
        # set after start() so reconnect() tests can assert lifecycle
        self.started: bool = False
        self.started_loop: Optional[asyncio.AbstractEventLoop] = None
        self.stopped: bool = False

    # ----- test-side controls -----

    def inject_error(self, method: str, exc: Exception) -> None:
        """Cause the next call to ``method`` to raise ``exc``.

        Stack-style: multiple injections on the same method consume in
        FIFO order so a test can model a transient daemon hiccup
        followed by recovery.
        """
        self.next_errors.setdefault(method, []).append(exc)

    def hold_ack(self, envelope_hash: bytes) -> None:
        """Make ``start_resending_encrypted_message`` for this envelope
        hang forever, modelling a stuck send/recv that never receives
        its ACK from the courier."""
        self.pending_acks.add(envelope_hash)

    def release_ack(self, envelope_hash: bytes) -> None:
        self.pending_acks.discard(envelope_hash)

    def add_courier(
        self, identity_hash: Optional[bytes] = None, queue_id: bytes = b"+courier",
    ) -> Tuple[bytes, bytes]:
        if identity_hash is None:
            identity_hash = secrets.token_bytes(32)
        entry = (identity_hash, queue_id)
        self.couriers.append(entry)
        return entry

    def remove_courier(self, identity_hash: bytes) -> None:
        self.couriers = [c for c in self.couriers if c[0] != identity_hash]

    def courier_destinations(self) -> List[bytes]:
        return [c[0] for c in self.couriers]

    def find_services(self, capability: str) -> List[FakeServiceDescriptor]:
        """Stand-in for `katzenpost_thinclient.find_services` that yields
        FakeServiceDescriptors derived from the current courier list.
        Bound into `network.py` via monkeypatch in conftest."""
        if capability != "courier":
            return []
        return [
            FakeServiceDescriptor(identity_hash=h, queue_id=q)
            for (h, q) in self.couriers
        ]

    def pre_store(
        self,
        *,
        write_cap: bytes,
        message_box_index: bytes,
        plaintext: bytes,
    ) -> None:
        """Pre-populate the box store as if the box had already been
        written. Useful for setting up the receive path in isolation
        from the send path."""
        self.box_store[(write_cap[32:], message_box_index)] = plaintext

    # ----- internal -----

    def _maybe_raise(self, method: str) -> None:
        queue = self.next_errors.get(method)
        if queue:
            raise queue.pop(0)

    def _bump_index(self, message_box_index: bytes) -> bytes:
        """Advance the BACAP Idx64 counter in the opaque 104-byte blob by
        one, matching the convention `create_new_keypair` enforces (high
        bit cleared, little-endian u64 in the first 8 bytes)."""
        cur = struct.unpack("<Q", message_box_index[:8])[0]
        nxt = (cur + 1) & 0x7FFFFFFFFFFFFFFF
        return struct.pack("<Q", nxt) + message_box_index[8:]

    def _record(self, method: str, **kwargs: Any) -> None:
        self.call_log.append((method, kwargs))

    def last_call(self, method: str) -> Dict[str, Any]:
        for name, kwargs in reversed(self.call_log):
            if name == method:
                return kwargs
        raise AssertionError(f"no {method!r} call recorded")

    def call_count(self, method: str) -> int:
        return sum(1 for name, _ in self.call_log if name == method)

    # ----- ThinClient surface used by network.py -----

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.started = True
        self.started_loop = loop

    def stop(self) -> None:
        self.stopped = True

    def is_connected(self) -> bool:
        return self.started and not self.stopped

    async def new_keypair(self, seed: bytes) -> KeypairResult:
        self._record("new_keypair", seed=seed)
        self._maybe_raise("new_keypair")
        write_cap, read_cap = create_new_keypair(seed)
        kp = KeypairResult(
            write_cap=write_cap,
            read_cap=read_cap,
            first_message_index=read_cap[-104:],
        )
        self.keypairs.append(kp)
        return kp

    async def encrypt_write(
        self,
        plaintext: bytes,
        write_cap: bytes,
        message_box_index: bytes,
    ) -> EncryptWriteResult:
        self._record(
            "encrypt_write",
            plaintext=plaintext,
            write_cap=write_cap,
            message_box_index=message_box_index,
        )
        self._maybe_raise("encrypt_write")
        envelope_hash = secrets.token_bytes(32)
        next_idx = self._bump_index(message_box_index)
        self.envelopes[envelope_hash] = _Envelope(
            is_write=True,
            box_id=write_cap[32:],
            message_box_index=message_box_index,
            next_message_box_index=next_idx,
            plaintext=plaintext,
            cap=write_cap,
        )
        return EncryptWriteResult(
            message_ciphertext=b"WCT:" + envelope_hash,
            envelope_descriptor=b"WED:" + envelope_hash,
            envelope_hash=envelope_hash,
            next_message_box_index=next_idx,
        )

    async def encrypt_read(
        self, read_cap: bytes, message_box_index: bytes,
    ) -> EncryptReadResult:
        self._record(
            "encrypt_read",
            read_cap=read_cap,
            message_box_index=message_box_index,
        )
        self._maybe_raise("encrypt_read")
        envelope_hash = secrets.token_bytes(32)
        next_idx = self._bump_index(message_box_index)
        self.envelopes[envelope_hash] = _Envelope(
            is_write=False,
            box_id=read_cap,
            message_box_index=message_box_index,
            next_message_box_index=next_idx,
            plaintext=b"",
            cap=read_cap,
        )
        return EncryptReadResult(
            message_ciphertext=b"RCT:" + envelope_hash,
            envelope_descriptor=b"RED:" + envelope_hash,
            envelope_hash=envelope_hash,
            next_message_box_index=next_idx,
        )

    async def start_resending_encrypted_message(
        self,
        read_cap: "bytes | None" = None,
        write_cap: "bytes | None" = None,
        message_box_index: "bytes | None" = None,
        reply_index: "int | None" = None,
        envelope_descriptor: "bytes | None" = None,
        message_ciphertext: "bytes | None" = None,
        envelope_hash: "bytes | None" = None,
        no_retry_on_box_id_not_found: bool = False,
        no_idempotent_box_already_exists: bool = False,
    ) -> StartResendingResult:
        self._record(
            "start_resending_encrypted_message",
            envelope_hash=envelope_hash,
            write_cap=write_cap,
            read_cap=read_cap,
            message_box_index=message_box_index,
            envelope_descriptor=envelope_descriptor,
            message_ciphertext=message_ciphertext,
        )
        self._maybe_raise("start_resending_encrypted_message")
        if envelope_hash in self.pending_acks:
            await asyncio.Event().wait()  # never set; models stuck ACK
        env = self.envelopes.get(envelope_hash)
        courier = self.couriers[0] if self.couriers else (None, None)
        if env is None:
            raise BoxIDNotFoundError()
        if env.is_write:
            self.box_store[(env.box_id, env.message_box_index)] = env.plaintext
            return StartResendingResult(
                plaintext=b"",
                courier_identity_hash=courier[0],
                courier_queue_id=courier[1],
            )
        stored = self.box_store.get((env.box_id, env.message_box_index))
        if stored is None:
            raise BoxIDNotFoundError()
        return StartResendingResult(
            plaintext=stored,
            courier_identity_hash=courier[0],
            courier_queue_id=courier[1],
        )

    async def get_message_box_index_counter(
        self, message_box_index: bytes,
    ) -> int:
        self._record(
            "get_message_box_index_counter",
            message_box_index=message_box_index,
        )
        self._maybe_raise("get_message_box_index_counter")
        return struct.unpack("<Q", message_box_index[:8])[0]

    def pki_document(self) -> Dict[str, Any]:
        return self._pki_doc

    def get_all_couriers(self) -> List[Tuple[bytes, bytes]]:
        return list(self.couriers)
