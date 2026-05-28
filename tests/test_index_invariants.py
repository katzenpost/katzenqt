"""BACAP index-tracking invariants that katzenqt must uphold.

Each test here pins one invariant whose violation is plausibly the cause of
the intermittent MKEM decryption failures ThreeBitHacker observed when
katzenqt drives the new capability-based Pigeonhole API. See
`/home/human/.claude/plans/please-make-a-plan-parsed-plum.md` for the full
context and rationale.

The tests operate on an ephemeral SQLite (set up via the root `conftest.py`)
and avoid touching the live thin client. Message-box-index bytes are
treated as opaque 104-byte blobs with a LE u64 Idx64 in the first 8 bytes,
matching the layout `hpqc/bacap/bacap.go` commits to.
"""
import asyncio
import struct
import uuid

import pytest
from sqlmodel import Session, SQLModel, select

from katzenqt import network, persistent


# ---------------------------------------------------------------------------
# Stub for the ThinClient's get_message_box_index_counter dependency.
# ---------------------------------------------------------------------------

class _FakeConnection:
    """Minimal stand-in for ThinClient in mark_sent tests.

    mark_sent only needs `get_message_box_index_counter(blob) -> int`. We
    compute it locally in the test using the same layout the daemon does
    (first 8 bytes little-endian uint64) so the tests don't depend on a
    running kpclientd. The layout knowledge is confined to this stub.
    """

    async def get_message_box_index_counter(self, message_box_index: bytes) -> int:
        return int.from_bytes(message_box_index[:8], "little")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mbi(idx: int, blinding: bytes = b"") -> bytes:
    """Build a 104-byte MessageBoxIndex blob with Idx64=idx.

    The blinding/encryption/HKDF tail is arbitrary for these tests (katzenqt
    treats the tail as opaque). Callers who want to prove two blobs are
    *distinct* at a BACAP-like level of granularity can pass a unique
    `blinding` prefix.
    """
    tail = (blinding + b"\x00" * 96)[:96]
    return struct.pack("<Q", idx) + tail


def _insert_write_stream(*, bacap_stream: uuid.UUID, next_idx: int):
    """Insert a (WriteCapWAL, linked PlaintextWAL) pair ready for mark_sent().

    Returns detached but attribute-populated Python objects (via
    `expire_on_commit=False`) so callers can keep reading .id, .bacap_stream,
    .next_index, etc. after the session closes.
    """
    wcw = persistent.WriteCapWAL(
        id=bacap_stream,
        write_cap=b"W" * 168,
        next_index=mbi(next_idx),
    )
    pwal = persistent.PlaintextWAL(
        id=uuid.uuid4(),
        after_id=None,
        after_stream=None,
        bacap_stream=bacap_stream,
        conversation_id=1,
        bacap_payload=b"Fhello",
    )
    with Session(persistent._engine_sync, expire_on_commit=False) as sess:
        sess.add(wcw)
        sess.add(pwal)
        sess.commit()
    return wcw, pwal


def _insert_mw(mw: persistent.MixWAL) -> None:
    """Persist an MW with expire_on_commit=False so its fields stay readable."""
    with Session(persistent._engine_sync, expire_on_commit=False) as sess:
        sess.add(mw)
        sess.commit()


def _make_mw(
    *, bacap_stream: uuid.UUID, pwal_id: uuid.UUID,
    current_idx: int, next_idx: int, is_read: bool = False,
    envelope_hash: bytes | None = None,
) -> persistent.MixWAL:
    return persistent.MixWAL(
        id=uuid.uuid4(),
        bacap_stream=bacap_stream,
        plaintextwal=pwal_id,
        envelope_hash=envelope_hash or uuid.uuid4().bytes + b"\x00" * 16,
        destination=b"C" * 32,
        encrypted_payload=b"ciphertext",
        envelope_descriptor=b"descriptor",
        current_message_index=mbi(current_idx),
        next_message_index=mbi(next_idx),
        is_read=is_read,
    )


# ---------------------------------------------------------------------------
# Invariant 1: WriteCapWAL.next_index advances by exactly one per mark_sent.
# ---------------------------------------------------------------------------

def test_mark_sent_advances_by_exactly_one():
    """After mark_sent, wcw.next_index must equal mw.next_message_index."""
    bacap_stream = uuid.uuid4()
    wcw, pwal = _insert_write_stream(bacap_stream=bacap_stream, next_idx=42)

    mw = _make_mw(
        bacap_stream=bacap_stream, pwal_id=pwal.id,
        current_idx=42, next_idx=43,
    )
    _insert_mw(mw)

    resend_queue: set = {bacap_stream}

    async def _do():
        await persistent.SentLog.mark_sent(_FakeConnection(), mw, resend_queue)

    asyncio.run(_do())

    with Session(persistent._engine_sync) as sess:
        reloaded = sess.get(persistent.WriteCapWAL, bacap_stream)
        assert reloaded is not None
        got_idx = struct.unpack("<Q", reloaded.next_index[:8])[0]
        assert got_idx == 43, (
            f"mark_sent should advance wcw.next_index to Idx64=43, got {got_idx}"
        )
        # mark_sent also deletes the MW and pwal
        assert sess.get(persistent.MixWAL, mw.id) is None
        assert sess.get(persistent.PlaintextWAL, pwal.id) is None
        assert sess.exec(select(persistent.SentLog)).first() is not None


# ---------------------------------------------------------------------------
# Invariant 2: mark_sent refuses to regress wcw.next_index.
# ---------------------------------------------------------------------------

def test_mark_sent_refuses_regression_on_pwal_present_path():
    """If another drain has already advanced wcw.next_index past the MW's
    next_message_index, mark_sent must NOT clobber it back to the stale value.

    Current behavior: the pwal-present branch unconditionally assigns
    `wcw.next_index = mw.next_message_index`, which can regress the counter.
    The pwal-absent branch at `persistent.py:165-169` already has the
    regression check; it must cover the pwal-present path too.
    """
    bacap_stream = uuid.uuid4()
    # Another drain already advanced the writer two steps past what this
    # particular MW knows about: wcw.next_index == 44.
    wcw, pwal = _insert_write_stream(bacap_stream=bacap_stream, next_idx=44)

    # This MW was created when wcw was at idx 42; its next_message_index is 43.
    mw = _make_mw(
        bacap_stream=bacap_stream, pwal_id=pwal.id,
        current_idx=42, next_idx=43,
    )
    _insert_mw(mw)

    resend_queue: set = {bacap_stream}

    async def _do():
        await persistent.SentLog.mark_sent(_FakeConnection(), mw, resend_queue)

    asyncio.run(_do())

    with Session(persistent._engine_sync) as sess:
        reloaded = sess.get(persistent.WriteCapWAL, bacap_stream)
        assert reloaded is not None
        got_idx = struct.unpack("<Q", reloaded.next_index[:8])[0]
        assert got_idx == 44, (
            f"mark_sent must not regress wcw.next_index from 44 to 43; "
            f"got Idx64={got_idx}. Expected unconditional regression guard."
        )


# ---------------------------------------------------------------------------
# Invariant 7: find_resendable's resend_queue filter targets bacap_stream.
# ---------------------------------------------------------------------------

def test_find_resendable_filter_targets_bacap_stream_not_pwal_id():
    """__resend_queue holds bacap_stream UUIDs. The SQL filter in
    find_resendable must exclude PWALs whose bacap_stream is in the queue.

    Current behavior: the filter incorrectly compares against
    `PlaintextWAL.id`, which essentially never matches a bacap_stream UUID,
    so the SQL guard is a no-op. This test forces the fixable condition.
    """
    bacap_stream_busy = uuid.uuid4()
    bacap_stream_free = uuid.uuid4()

    with Session(persistent._engine_sync) as sess:
        # Both streams are fully provisioned (read_cap + write_cap populated)
        # so that the find_resendable CTEs allow them through. We only care
        # about the resend_queue filter here.
        wcw_busy = persistent.WriteCapWAL(
            id=bacap_stream_busy, write_cap=b"W" * 168, next_index=mbi(1),
        )
        wcw_free = persistent.WriteCapWAL(
            id=bacap_stream_free, write_cap=b"W" * 168, next_index=mbi(1),
        )
        rcw_busy = persistent.ReadCapWAL(
            id=uuid.uuid4(), write_cap_id=bacap_stream_busy,
            read_cap=b"R" * 136, next_index=mbi(1),
        )
        rcw_free = persistent.ReadCapWAL(
            id=uuid.uuid4(), write_cap_id=bacap_stream_free,
            read_cap=b"R" * 136, next_index=mbi(1),
        )
        pwal_busy = persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=bacap_stream_busy,
            conversation_id=1, bacap_payload=b"Fbusy",
        )
        pwal_free = persistent.PlaintextWAL(
            id=uuid.uuid4(), bacap_stream=bacap_stream_free,
            conversation_id=1, bacap_payload=b"Ffree",
        )
        for o in (wcw_busy, wcw_free, rcw_busy, rcw_free, pwal_busy, pwal_free):
            sess.add(o)
        sess.commit()

        # The resend_queue guard is supposed to say "don't pick up another
        # PWAL whose bacap_stream is already being drained".
        resend_queue = {bacap_stream_busy}
        rows = list(sess.exec(persistent.PlaintextWAL.find_resendable(resend_queue)))
        returned_streams = {row.bacap_stream for row in rows}

        assert bacap_stream_busy not in returned_streams, (
            f"find_resendable must exclude PWALs whose bacap_stream is in "
            f"resend_queue; got streams={returned_streams}"
        )
        assert bacap_stream_free in returned_streams, (
            f"find_resendable must include PWALs whose bacap_stream is not "
            f"in resend_queue; got streams={returned_streams}"
        )


# ---------------------------------------------------------------------------
# Invariant 6: late-binding lambda in send_resendable_plaintexts is fixed.
# ---------------------------------------------------------------------------

def test_send_resendable_plaintexts_has_no_late_bound_lambda():
    """The on_error callback `lambda: __resend_queue.discard(pwal.bacap_stream)`
    captures the loop variable by reference, so if an earlier iteration's
    task errors, the lambda fired at that iteration will discard the LAST
    iteration's bacap_stream instead.

    This test is a source-level regression guard: the broken pattern must
    not be present in network.py. The fix (default-arg capture) is:

        on_error(t, lambda s=pwal.bacap_stream: __resend_queue.discard(s))
    """
    import inspect
    src = inspect.getsource(network.send_resendable_plaintexts)
    assert "lambda: __resend_queue.discard(pwal.bacap_stream)" not in src, (
        "send_resendable_plaintexts uses a late-binding lambda that closes "
        "over the loop variable `pwal`; if an inner iteration's task fails, "
        "the WRONG bacap_stream is discarded from __resend_queue. Replace "
        "with a default-arg capture:\n"
        "    on_error(t, lambda s=pwal.bacap_stream: __resend_queue.discard(s))"
    )


# ---------------------------------------------------------------------------
# Invariant 8: after_stream gate releases only when no PWAL remains on the
# referenced bacap_stream. The previous SQL compared ``after_stream`` (a
# bacap_stream UUID) against ``sent_cte`` (a set of PWAL IDs), two
# disjoint identifier spaces, so the indirection PWAL of any multi-box
# send was permanently un-resendable and the send timed out.
# ---------------------------------------------------------------------------


def test_find_resendable_after_stream_gate_releases_when_substream_drained():
    """Stage a multi-box send fixture: three substream chunks and one
    indirection PWAL on the parent stream with after_stream=substream_uuid.
    While any substream chunk remains in plaintextwal the indirection
    must NOT be resendable. Once they are all mark_sent (deleted from
    plaintextwal and present in sentlog) the indirection MUST appear
    in find_resendable's result set."""
    substream_uuid = uuid.uuid4()
    parent_stream_uuid = uuid.uuid4()
    substream_rcw_id = uuid.uuid4()

    pwal_c1_id = uuid.uuid4()
    pwal_c2_id = uuid.uuid4()
    pwal_f_id = uuid.uuid4()
    pwal_indirection_id = uuid.uuid4()

    with Session(persistent._engine_sync) as sess:
        # Substream WCW+RCW, both populated so the cap CTEs admit the rows.
        sess.add(persistent.WriteCapWAL(
            id=substream_uuid, write_cap=b"W" * 168, next_index=mbi(1),
        ))
        sess.add(persistent.ReadCapWAL(
            id=substream_rcw_id, write_cap_id=substream_uuid,
            read_cap=b"R" * 136, next_index=mbi(1),
        ))
        # Parent stream's WCW+RCW (the conv's own caps).
        sess.add(persistent.WriteCapWAL(
            id=parent_stream_uuid, write_cap=b"W" * 168, next_index=mbi(1),
        ))
        sess.add(persistent.ReadCapWAL(
            id=uuid.uuid4(), write_cap_id=parent_stream_uuid,
            read_cap=b"R" * 136, next_index=mbi(1),
        ))
        # Substream chunks linked C -> C -> F via after_id.
        sess.add(persistent.PlaintextWAL(
            id=pwal_c1_id, after_id=None, after_stream=None,
            bacap_stream=substream_uuid, conversation_id=1,
            bacap_payload=b"C" + b"a" * 32,
        ))
        sess.add(persistent.PlaintextWAL(
            id=pwal_c2_id, after_id=pwal_c1_id, after_stream=None,
            bacap_stream=substream_uuid, conversation_id=1,
            bacap_payload=b"C" + b"b" * 32,
        ))
        sess.add(persistent.PlaintextWAL(
            id=pwal_f_id, after_id=pwal_c2_id, after_stream=None,
            bacap_stream=substream_uuid, conversation_id=1,
            bacap_payload=b"F" + b"c" * 32,
        ))
        # Indirection PWAL on the parent stream: waits for substream drain.
        sess.add(persistent.PlaintextWAL(
            id=pwal_indirection_id, after_id=None,
            after_stream=substream_uuid,
            bacap_stream=parent_stream_uuid, conversation_id=1,
            bacap_payload=b"", indirection=substream_rcw_id,
        ))
        sess.commit()

        # While substream chunks remain, the indirection is gated out.
        rows = list(sess.exec(persistent.PlaintextWAL.find_resendable(set())))
        returned = {row.id for row in rows}
        assert pwal_indirection_id not in returned, (
            f"indirection PWAL must NOT be resendable while substream chunks "
            f"remain in plaintextwal; got rows={returned}"
        )

        # Drain the substream: mark all chunks sent and delete them.
        for pid in (pwal_c1_id, pwal_c2_id, pwal_f_id):
            sess.add(persistent.SentLog(id=pid))
            sess.delete(sess.get(persistent.PlaintextWAL, pid))
        sess.commit()

        rows = list(sess.exec(persistent.PlaintextWAL.find_resendable(set())))
        returned = {row.id for row in rows}
        assert pwal_indirection_id in returned, (
            f"indirection PWAL must be resendable once its after_stream "
            f"({substream_uuid}) has no remaining PWALs; got rows={returned}"
        )


def test_late_binding_pattern_is_really_a_python_hazard():
    """Demonstrate the late-binding semantics so the test above is
    calibrated. If this ever fails, Python's loop-variable binding rules
    have changed and the fix in send_resendable_plaintexts may no longer
    be needed.
    """
    lambdas = []
    for item in ("a", "b", "c"):
        lambdas.append(lambda: item)  # noqa: B023 — intentional
    assert [fn() for fn in lambdas] == ["c", "c", "c"]

    # And the same loop with the default-arg fix:
    lambdas = []
    for item in ("a", "b", "c"):
        lambdas.append(lambda it=item: it)
    assert [fn() for fn in lambdas] == ["a", "b", "c"]
