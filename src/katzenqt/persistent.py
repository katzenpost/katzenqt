import asyncio
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base
#from pydantic import BaseModel, ConfigDict, Field
import importlib.resources
from pathlib import Path
import alembic.config
import alembic.command
import alembic.context
import uuid
from contextlib import asynccontextmanager
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine, UniqueConstraint, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
import sqlalchemy
count = sqlalchemy.func.count
import aiosqlite # https://pypi.org/project/aiosqlite/
from typing import TYPE_CHECKING, AsyncIterator
from .katzen_util import create_task
if TYPE_CHECKING:
    from typing import AsyncContextManager
    import sqlmodel
from alembic import context
import logging
import os
from threading import Lock

logger = logging.getLogger("katzen.persistent")


# conversation_order is assigned by a scalar subquery evaluated at INSERT time
# (autoflush, right before COMMIT). Every ConversationLog append site funnels
# through the single writer coroutine on the io loop — the GUI send path hops
# in via run_in_io (append_outbound_chat), new_conversation via
# run_in_io(_commit_new_conversation), and the receive/voucher paths already
# run on the io loop — so the aiosqlite session is never shared across two
# loops. (The GUI-thread _engine_sync circuit is gone: _commit_new_conversation
# replaced the one Qt-thread commit site that previously carved itself out
# here.) The appends still serialise their "count, insert, commit" critical
# section with the per-conversation async poll lock below: two in-flight
# appends to the same conversation cannot read the same count and trip
# UniqueConstraint(conversation_id, conversation_order), silently dropping a
# message (or failing an induction that already succeeded on the wire). The
# lock is a non-blocking acquire-and-poll so a same-conversation waiter on the
# *same* loop can never deadlock the loop thread that holds the lock mid-await
# — and, being cross-loop capable, it stays correct even if a future caller
# skips the funnel.
__conversation_log_order_locks: dict[int, Lock] = {}
__conversation_log_order_locks_guard = Lock()

# Poll interval for conversation_log_order_lock's non-blocking acquire loop.
# Small relative to a human-paced chat send, so contention adds no
# perceptible latency; large enough that an uncontended waiter isn't
# spinning the loop needlessly.
_CONVERSATION_LOG_ORDER_LOCK_POLL_S = 0.005

@asynccontextmanager
async def conversation_log_order_lock(conversation_id: int) -> AsyncIterator[None]:
    """Hold the write lock for a conversation's log-append critical section.

    Use it around the ``sess.add(ConversationLog(...))`` .. ``await
    sess.commit()`` window at every site that assigns ``conversation_order``.

    This has to be an async context manager, not a blocking one: two of the
    three call sites run as asyncio tasks that can share an event loop with
    another task appending to the *same* conversation (drain_mixwal2 fans
    out one create_task() per readable MixWAL row with no await between
    calls, so two peers of one conversation routinely land on the same
    io-thread loop in the same pass). A blocking threading.Lock.acquire()
    here would let one task's genuine await (e.g. sess.commit()) while
    holding the lock stall a second task's acquire() call on the *same* OS
    thread -- and a blocked acquire() never lets that thread's loop run the
    first task's continuation, which is what would release the lock: a
    permanent same-thread deadlock. Polling with a non-blocking acquire()
    and sleeping between attempts keeps every wait cooperative, whether the
    other holder is on this loop or, via the shared threading.Lock, a
    different loop's thread entirely.
    """
    with __conversation_log_order_locks_guard:
        lock = __conversation_log_order_locks.setdefault(conversation_id, Lock())
    # Acquire before entering the try: a task cancelled while parked in the
    # poll sleep holds nothing, and an unconditional finally release() there
    # would either raise RuntimeError on a free lock (on top of the
    # CancelledError) or silently unlock another same-conversation task's
    # lock mid-critical-section, reopening the count/insert race.
    while not lock.acquire(blocking=False):
        await asyncio.sleep(_CONVERSATION_LOG_ORDER_LOCK_POLL_S)
    try:
        yield
    finally:
        lock.release()


def next_conversation_order(conversation_id: int):
    """Scalar subquery for the next ``conversation_order`` value: a live
    COUNT evaluated at INSERT/COMMIT time. Shared by every ConversationLog
    append site so a future change to how the order is derived only needs
    to be made once."""
    return (
        select(count())
        .select_from(ConversationLog)
        .where(ConversationLog.conversation_id == conversation_id)
        .scalar_subquery()
    )


async def append_outbound_chat(
    *,
    conversation_id: int,
    conversation_peer_id: int,
    new_write_caps: list[uuid.UUID],
    db_entries: list[SQLModel],
    payload: bytes,
    final_pwal_id: uuid.UUID | None = None,
) -> None:
    """Append one outbound chat message's WAL rows and its ConversationLog entry.

    This is the GUI send path's writer: it runs on the io loop (invoked via
    ``MainWindow.iothread.run_in_io``) so the aiosqlite session is never
    shared across the Qt loop and the io loop, under the per-conversation
    writer lock, so the ``conversation_order`` count-subquery (evaluated at
    COMMIT) is stamped atomically with respect to the receive/voucher appends.
    """
    async with conversation_log_order_lock(conversation_id):
        async with asession() as sess:
            for cap_uuid in new_write_caps:
                sess.add(WriteCapWAL(id=cap_uuid))
            for obj in db_entries:
                sess.add(obj)
            sess.add(ConversationLog(
                conversation_id=conversation_id,
                conversation_peer_id=conversation_peer_id,
                conversation_order=next_conversation_order(conversation_id),
                payload=payload,
                network_status=1,
                outgoing_pwal=final_pwal_id,
            ))
            await sess.commit()


def _resolve_alembic_ini() -> Path:
    """Locate the ``alembic.ini`` shipped as package data.

    Resolved through ``importlib.resources`` so it works the same for
    editable and copy installs, independent of the repository location.
    """
    return Path(str(importlib.resources.files("katzenqt") / "data" / "alembic.ini"))


_alembic_cfg = alembic.config.Config(_resolve_alembic_ini())

xdg_data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home()/".local"/"share")
xdg_data_home.mkdir(parents=True,exist_ok=True)
app_data = xdg_data_home / "katzenqt"
app_data.mkdir(exist_ok=True, mode=0o700)

_state_name = os.getenv("KQT_STATE", "") or "katzen"
state_file: Path = app_data / f"{_state_name}.sqlite3"
_sql_url = f"sqlite+aiosqlite:///{ state_file }"
logger.info("sql url: %s", _sql_url)
# pool_size is generous on purpose: sqlite itself serialises actual writes
# (via WAL + busy_timeout above), so this pool isn't adding write throughput.
# What it avoids is SQLAlchemy's own default QueuePool (size 5 + overflow 10)
# queuing a checkout behind its 30s pool_timeout before a caller ever reaches
# sqlite's much shorter busy wait — under a write burst (many concurrent
# asyncio.to_thread sessions plus the io loop's own) that pool-level queue,
# not sqlite's lock, becomes the thing that stalls the Qt GUI thread's own
# settings writes for tens of seconds instead of a bounded couple of ticks.
_engine = create_async_engine(_sql_url, echo=False, future=True, pool_size=1000)
_engine_sync = create_engine(_sql_url.replace('+aiosqlite://','://'), echo=False, pool_size=1000)


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable WAL and a short busy timeout on every pooled connection.

    Without busy_timeout, any write that finds another writer holding the
    sqlite write lock fails immediately with ``database is locked``; the
    GUI send and io receive threads contend on the same file, so a burst
    wedges whatever drain task happened to be committing. WAL keeps readers
    out of the writers' way and turns most of that contention into waits.
    The timeout must stay SMALL: the sync engine is used both from worker
    threads (mark_sent's asyncio.to_thread calls) and directly from the Qt
    GUI thread (settings writes), so a large value freezes whichever of
    those threads is waiting for its full duration on every contended
    write. 250ms absorbs ordinary micro-
    contention yet bounds any loop/GUI stall to a couple of timer ticks;
    sustained contention degrades to the drains' give_up-and-retry path
    instead of a blocking stall. WAL is file-persistent; busy_timeout is per
    connection, hence the connect event rather than engine-level setup.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=250")
    cursor.close()
    # journal_mode=WAL lazily creates the -wal/-shm sidecars on first write,
    # under the process umask rather than inheriting the main file's 0600 —
    # restrict them too, now that they're guaranteed to exist.
    _restrict_state_file_perms(state_file)


sa.event.listens_for(_engine.sync_engine, "connect")(_set_sqlite_pragmas)
sa.event.listens_for(_engine_sync, "connect")(_set_sqlite_pragmas)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = SQLModel.metadata
metadata.naming_convention = NAMING_CONVENTION


@asynccontextmanager
async def asession() -> "AsyncContextManager[sqlmodel.ext.asyncio.session.AsyncSession]":
    """Opens a sqlmodel.ext.asyncio.session.AsyncSession

    Connection acquisition and release are shielded from task
    cancellation. A task cancelled while aiosqlite is still opening
    the database (or while the session is being closed) abandons the
    raw sqlite connection unclosed; shutdown cancelling the network
    loops did exactly that, and the garbage collector later reported
    it as an "unclosed database" ResourceWarning. The shields let the
    checkout and the close run to completion before the cancellation
    unwinds.
    """
    session = AsyncSession(_engine)
    acquire = asyncio.ensure_future(session.connection())
    try:
        await asyncio.shield(acquire)
        yield session
    except asyncio.CancelledError:
        if not acquire.done():
            await acquire
        raise
    finally:
        close = asyncio.ensure_future(session.close())
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            await close
            raise

def _restrict_state_file_perms(path: Path) -> None:
    """Tighten the on-disk state database, and its WAL/SHM sidecars if
    present, to owner-only (0600).

    The state directory is already 0700, but each file is created with the
    process umask, so on a permissive umask any of them can be group- or
    world-readable. journal_mode=WAL means uncheckpointed writes -- BACAP
    caps, signing keys, message plaintext -- can sit in the -wal/-shm
    sidecars, not just the main file, so all three need clamping. Best-
    effort: a missing file or a filesystem that does not honour chmod is not
    fatal to startup."""
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            if candidate.is_file():
                os.chmod(candidate, 0o600)
        except OSError as exc:  # pragma: no cover - platform/filesystem dependent
            logger.warning("could not restrict permissions on %s: %s", candidate, exc)


def init_and_migrate():
    """Initialize database and migrates application schema.

    This MUST be called on application startup.
    """
    alembic.command.upgrade(_alembic_cfg, "head")
    _restrict_state_file_perms(state_file)

def id_field(table_name: str):
    sequence = sa.Sequence(f"{table_name}_id_seq")
    return Field(
        default=None,
        primary_key=True,
        sa_column_args=[sequence],
        sa_column_kwargs={"server_default": sequence.next_value()},
    )

class AppSetting(SQLModel, table=True):
    id: str = Field(primary_key=True)  # name of the setting
    type: str = Field(nullable=False)  # "str" or "int", I guess
    value: str = Field(nullable=True)  # value or NULL

class MixWAL(SQLModel, table=True):
    """
    Stores EncryptWriteResult/EncryptReadResult from ThinClient.encrypt_read() and encrypt_write()
    for resending to the courier.
    We need to feed all of these into ThinClient.start_resending_encrypted_message

    TODO: Ok so say we want to send a message that spans a few BACAP boxes:
    - We create a new channel: EPH
    - We WriteChannel(EPH) them
      - We get WriteChannelReply and get a sequence that we put in MixWAL
    - We should also WriteChannel(orig):
      - Go look at EPH
      - This one MUST NOT be sent before all the EPH writes are done.
      - We should have a logical ordering that we can ORDER BY / GROUP BY
      - We must ensure we do not launch a conflicting WriteChannel(orig)
       - but we're allowed to complete operations in any order
       - ergo: should not do WriteChannel(orig) until the operation has succeeded.
         - which means we should have a plaintext log of messages we want to send,
           and those should reference an operation ID here.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # TODO should we store a uuid.UUID for the PlaintextWAL message?
    plaintextwal: uuid.UUID = Field(nullable=True, default=None, index=True, foreign_key="plaintextwal.id")  # TODO constraint to make sure is_read=False for these
    bacap_stream: uuid.UUID = Field(unique=True) # There can only be one active write per stream
    # it can't be a foreign_key because we track ReadCapWAL+WriteCapWAL separately.
    envelope_hash: bytes = Field(unique=True)
    encrypted_payload: bytes     # send_message_payload
    envelope_descriptor : bytes #  envelope private key
    current_message_index: bytes = Field(min_length=104, max_length=104)
    next_message_index: bytes = Field(min_length=104, max_length=104)
    is_read : bool
    @classmethod
    def get_new(cls, except_these:"Set[uuid.UUID]"):
        return select(cls).where(cls.bacap_stream.not_in(except_these))
    @classmethod
    async def resend_queue_from_disk(cls) -> "Set[uuid.UUID]":
        # TODO something needs to restore the connection.ack_queues listeners, which means we need to know the message_id
        # TODO associated with MixWALs that we send out
        bacap_streams = set()
        async with asession() as sess:
            for stream in await sess.exec(select(cls.bacap_stream)):
                bacap_streams.add(stream)
        return bacap_streams

class ReadCapWAL(SQLModel, table=True):
    id: uuid.UUID = Field(primary_key=True)
    write_cap_id : uuid.UUID | None = Field(foreign_key="writecapwal.id", index=True)
    read_cap: bytes | None = Field(None, min_length=136, max_length=136)
    next_index: bytes | None = Field(None, min_length=104, max_length=104)
    @classmethod
    async def get_by_bacap_stream(cls, stream: uuid.UUID):
        return (await sess.exec(select(cls).where(id=stream))).one()

class WriteCapWAL(SQLModel, table=True):
    id: uuid.UUID = Field(primary_key=True)
    write_cap: bytes | None = Field(None, min_length=168, max_length=168)
    next_index: bytes | None = Field(None, min_length=104, max_length=104)
    @classmethod
    def get_by_bacap_uuid(cls, uuid):
        # from typing import ClassVar
        # select_by_bacap_uuid: ClassVar[sa.select()] = sa.select(cls).where(cls.id==uuid)
        return sa.select(cls).where(cls.id==uuid)

class PendingVoucher(SQLModel, table=True):
    """An in-flight Contact Voucher handshake, durable across restarts.

    The voucher protocol is a fixed two-box exchange on a rendezvous stream
    (VoucherStream) derived from the ``voucher`` token. We do not push it through
    the chat MixWAL/PlaintextWAL loops; instead the dedicated helper in
    ``voucher.py`` drives the handshake and advances ``step`` on this row before
    each network step, so a crash mid-handshake can resume from the persisted
    state rather than starting over.

    A ``joiner`` row carries the secret key needed to open the inductor's sealed
    reply; an ``inductor`` row carries only the public rendezvous material. The
    salt itself never lands here: it is the daemon's to mint and Bob's to recover
    transiently at open time, and is never persisted.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    role: str = Field(description='"joiner" or "inductor"')
    conversation_id: int = Field(foreign_key="conversation.id", index=True,
        description="joiner: the conversation whose write cap is the MessageStream; "
                    "inductor: the group the joiner is being inducted into")
    step: str = Field(description="minted/published/awaiting/sealed/done")
    voucher: bytes = Field(description="the token; derives the VoucherStream")
    voucher_write_cap: bytes | None = Field(None, min_length=168, max_length=168)
    voucher_read_cap: bytes | None = Field(None, min_length=136, max_length=136)
    # joiner only: needed to MKEM-open the inductor's sealed reply at box 1.
    voucher_secret_key: bytes | None = Field(None)
    # box 1 index, learned from the box 0 encrypt result; the inductor writes
    # its sealed reply here and the joiner reads it back here.
    box1_index: bytes | None = Field(None, min_length=104, max_length=104)
    display_name: str | None = Field(None, description="joiner's own name, for the mint")
    peer_name: str | None = Field(None, description="inductor's name for the joining peer")

# Caps how many of mark_sent's asyncio.to_thread calls (below) run at once.
# Each can block a worker thread for up to busy_timeout (250ms) waiting on
# sqlite's write lock; unbounded, a burst of simultaneously-resendable writes
# (e.g. right after a reconnect) can saturate Python's shared default
# ThreadPoolExecutor and start queuing unrelated asyncio.to_thread callers
# elsewhere in the app behind these DB commits. Semaphore-over-worker-pool is
# this project's usual pattern for bounding a variable-throughput stream.
_MARK_SENT_THREAD_SEM = asyncio.Semaphore(8)


class SentLog(SQLModel, table=True):
    id: uuid.UUID = Field(primary_key=True)  # previously the UUID assigned in MixWAL
    @classmethod
    async def mark_sent(cls, connection, mw:MixWAL, resend_queue) -> int:
        """Add SentLog entry, delete corresponding MixWAL and PlaintextWAL entries.
        Also bump index so we don't just keep writing to the same index??

        `connection` is a ThinClient used to resolve BACAP Idx64 counters out
        of opaque MessageBoxIndex blobs via get_message_box_index_counter.
        TODO: cleanup WriteCapWAL/ReadCapWAL entries when we are done with them (not urgent, but eventually)

        Returns the Conversation.id for the MixWAL entry so UI can be updated.
        """
        conversation_id = None
        # Unconditional regression guard: another drain may have already
        # advanced wcw.next_index past our mw.next_message_index (e.g., a
        # retransmission's ACK arriving after a later message's ACK).
        # Clobbering it back would let the writer re-encrypt at an index that
        # was already written, and BACAP derives a unique key per index — the
        # mismatch surfaces as an MKEM/BACAP decrypt failure at the reader.
        # Read the wcw blob first in a small sync transaction, then resolve
        # the counters via the thinclient OUTSIDE the session so we don't
        # hold a DB transaction across an await. The sync transactions
        # themselves run on a worker thread: mark_sent is awaited directly on
        # the event-loop task that owns the write drain, so blocking sqlite
        # I/O here (with its fsync commits) would stall every other task on
        # the loop regardless of the busy timeout's size.
        async with _MARK_SENT_THREAD_SEM:
            precheck_next_blob = await asyncio.to_thread(
                _read_wcw_precheck, mw.bacap_stream,
            )
        if precheck_next_blob is not None:
            real_next = await connection.get_message_box_index_counter(precheck_next_blob)
            our_next = await connection.get_message_box_index_counter(mw.next_message_index)
            if real_next >= our_next:
                logger.warning(
                    "mark_sent: skipping stale ACK for bacap_stream=%s "
                    "(db next=%d >= our next=%d); not regressing index",
                    mw.bacap_stream, real_next, our_next,
                )
                # Clean up the stray MW so we don't re-drain it, and finish
                # the job for this message: the writer has already advanced
                # past our_next, so the boxes this envelope sealed were
                # written and we just got their ACK — the PWAL is proven
                # delivered. Finalize it here rather than trusting a later
                # MW to do it: in the crash-then-relaunch case (write drain
                # died mid-commit) no later MW exists, and without this the
                # message resends forever.
                async with _MARK_SENT_THREAD_SEM:
                    conversation_id = await asyncio.to_thread(
                        _finalize_stale_ack, mw.id, mw.plaintextwal,
                    )
                resend_queue.discard(mw.bacap_stream)
                return conversation_id
        # Resolve the diagnostic counters once so the commit-time print is
        # as cheap as a tuple format rather than two more thinclient calls.
        new_idx = our_next if precheck_next_blob is not None else (
            await connection.get_message_box_index_counter(mw.next_message_index)
        )
        async with _MARK_SENT_THREAD_SEM:
            conversation_id = await asyncio.to_thread(
                _mark_sent_txn,
                mw.id, mw.bacap_stream, mw.plaintextwal, mw.is_read,
                mw.next_message_index, new_idx,
                real_next if precheck_next_blob is not None else None,
            )
        resend_queue.discard(mw.bacap_stream)
        return conversation_id


async def peer_has_read_cap(
    session: "AsyncSession", conversation_id: int, read_cap: bytes,
) -> bool:
    """True if any peer of the conversation (the owner included) already
    holds this read cap.

    Read caps are a member's unique cryptographic identity, so this is the
    dedup key for the "already inducted" guard: a failed post-commit ack
    makes a naive retry re-run the induction, and without a guard that
    retry would add a second peer for the same person (the same hazard
    ``_already_has`` closes for the announcement path). Uses an explicit
    join query rather than relationship traversal: the receive and voucher
    paths call this from SQLAlchemy's async session, where touching a
    ``conv.peers`` lazy relationship raises ``MissingGreenlet``.
    """
    rows = (
        await session.exec(
            select(ReadCapWAL.read_cap)
            .join(
                ConversationPeer,
                ConversationPeer.read_cap_id == ReadCapWAL.id,
            )
            .join(
                ConversationPeerLink,
                ConversationPeerLink.conversation_peer_id
                == ConversationPeer.id,
            )
            .where(
                ConversationPeerLink.conversation_id == conversation_id,
                ReadCapWAL.read_cap == read_cap,
            )
        )
    ).all()
    return bool(rows)


async def own_read_cap(session: "AsyncSession", conversation) -> "bytes | None":
    """The conversation owner's salt-mutated read cap: the write cap's
    [32:] suffix once that is provisioned, else the unmutated
    ``rcapwal.read_cap``.

    Two call sites hand-wrote this same lookup in slightly different
    shapes (``_handle_introduction`` recognises an announcement about
    ourselves with it; ``_build_who_reply`` announces ourselves to a
    joiner with it), so a change to how the own cap is derived only needs
    to be made once. A freshly created conversation's write cap is filled
    in by the background provisioning loop shortly after creation, so None
    here is a transient early-state, not an error.

    It resolves the owner through columns and explicit ``session.get``
    alone — never through the ``conversation.own_peer`` relationship.
    ``own_peer`` is a ``lazy="selectin"`` relationship and is NOT
    eager-loaded when ``conversation`` is reached via ``peer.conversation``
    (the link-model path used by ``drain_mixwal_read_single``), so reading
    it here makes SQLAlchemy fall back to a synchronous lazy-load and
    raise ``MissingGreenlet`` inside the aiosqlite session.
    """
    own_peer_id = conversation.own_peer_id
    own_rcw = None
    if own_peer_id is not None:
        own_peer = await session.get(ConversationPeer, own_peer_id)
        if own_peer is not None:
            own_rcw = await session.get(ReadCapWAL, own_peer.read_cap_id)
    own_cap = own_rcw.read_cap if own_rcw is not None else None
    if conversation.write_cap is not None:
        wcw = await session.get(WriteCapWAL, conversation.write_cap)
        if wcw is not None and wcw.write_cap is not None:
            own_cap = wcw.write_cap[32:]
    return own_cap


async def wait_for_sent(pwal_id: uuid.UUID, *, deadline_s: float, poll_s: float = 0.25) -> bool:
    """Poll SentLog for ``pwal_id`` until it appears or ``deadline_s``
    elapses. Returns True if acked in time, False on timeout.

    Shared by every caller that needs to block until an outbound
    plaintext's ACK lands (voucher.py's introduction wait, the headless
    SEND step); each decides for itself what a timeout means (log and
    move on, vs. fail the whole action)."""
    deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < deadline:
        async with asession() as sess:
            hit = (await sess.exec(
                select(SentLog).where(SentLog.id == pwal_id)
            )).first()
        if hit is not None:
            return True
        await asyncio.sleep(poll_s)
    return False


def _read_wcw_precheck(bacap_stream) -> "bytes | None":
    """Return wcw.next_index for the stream (worker-thread helper for mark_sent)."""
    with Session(_engine_sync) as sess:
        wcw = sess.get(WriteCapWAL, bacap_stream)
        return wcw.next_index if wcw else None


def _ensure_sent_log_and_flip_status(sess, pwal: "PlaintextWAL") -> "int | None":
    """Idempotent-insert pwal's SentLog row, and flip its ConversationLog
    entry (if any) to sent. Shared by both mark_sent finalize paths.

    A pre-existing SentLog row (a prior ACK commit won, its PWAL deletion
    racing) is reused instead of re-inserted, so this never raises
    IntegrityError on the SentLog primary key.
    """
    if sess.get(SentLog, pwal.id) is None:
        sess.add(SentLog(id=pwal.id))
    conversation_id = None
    if pwal.bacap_payload[:1] in (b"F", b"I"):
        # This is either:
        #   I: an Indirection release pointing to something else
        #   F: a Final message
        # If it's at a top level, we would have a local ConversationLog entry already,
        # and we can update that to reflect that message has been sent.
        if convlog := sess.exec(select(ConversationLog).where(ConversationLog.outgoing_pwal == pwal.id)).first():
            conversation_id = convlog.conversation_id
            convlog.network_status = 2
            sess.add(convlog)
    return conversation_id


def _finalize_stale_ack(mw_id, plaintextwal_id) -> "int | None":
    """Stale/stray-MW finalize transaction (worker-thread helper for mark_sent).

    Runs when the regression guard detected the writer already advanced past
    our next index. Reaps the stray MixWAL, records SentLog, flips
    ConversationLog status, and commits — see mark_sent's stale-ACK comment
    for the intent.
    """
    conversation_id = None
    with Session(_engine_sync) as sess:
        stale_mw = sess.get(MixWAL, mw_id)
        if stale_mw is not None:
            sess.delete(stale_mw)
        pwal = sess.get(PlaintextWAL, plaintextwal_id)
        if pwal is not None:
            conversation_id = _ensure_sent_log_and_flip_status(sess, pwal)
            sess.delete(pwal)
        sess.commit()
    return conversation_id


def _mark_sent_txn(mw_id, bacap_stream, plaintextwal_id, is_read,
                   next_index, new_idx, real_next) -> "int | None":
    """Normal ACK finalize transaction (worker-thread helper for mark_sent).

    Records SentLog, advances WriteCapWAL, flips ConversationLog status, and
    reaps the MixWAL/PlaintextWAL rows. Idempotent against duplicate ACKs: a
    pre-existing SentLog row (a prior ACK commit won, its PWAL deletion
    racing) is reused instead of re-inserted, so this never raises
    IntegrityError on the SentLog primary key. The MW delete is null-guarded
    for the same race.
    """
    with Session(_engine_sync) as sess:
        pwal = sess.get(PlaintextWAL, plaintextwal_id)
        if not pwal:
            logger.warning(
                "mark_sent: pwal lookup failed for mw.plaintextwal=%s "
                "(is_read=%s); most likely a duplicate ACK for an MW "
                "whose PWAL was already reaped",
                plaintextwal_id, is_read,
            )
            if mw_row := sess.get(MixWAL, mw_id):
                sess.delete(mw_row)
                sess.commit()
            return None
        conversation_id = _ensure_sent_log_and_flip_status(sess, pwal)
        if wcw := sess.get(WriteCapWAL, bacap_stream):
            logger.debug("updating wcw: old=%s new=%s", real_next, new_idx)
            wcw.next_index = next_index
            sess.add(wcw)
        else:
            logger.error(
                "mark_sent: no WriteCapWAL for bacap_stream=%s; "
                "next_index not advanced", bacap_stream,
            )
        if bacap_stream != pwal.bacap_stream:
            logger.error("mw.bacap_stream doesn't match pwal.bacap_stream")
        if mw_row := sess.get(MixWAL, mw_id):
            sess.delete(mw_row)
        sess.delete(pwal)
        # TODO maybe update ConversationLog entry if we start tracking sent msgs in the UI
        sess.commit()
    return conversation_id


class PlaintextWAL(SQLModel, table=True):
    """Plaintext chunks of (bacap_payload) to insert into (bacap_stream).
    See models.py for SendOperation -> PlaintextWAL serialization details.
    These are removed once the MixWAL entries have been ACK'ed by a courier - see SentLog.mark_sent().
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    after_id: uuid.UUID | None = Field(
        foreign_key="plaintextwal.id",
        index=True,
        unique=True,
        description="""Topological ordering within a BACAP sequence."""
        """Chunks wait until there is no plaintextwal.id=plaintextwal.after_id entry left."""
    )
    after_stream: uuid.UUID | None = Field(
        foreign_key="plaintextwal.bacap_stream",
        index=True,
        description="""Topological ordering of BACAP sequences, for All-or-Nothing."""
        """ When (after_stream) IS NOT NULL, the chunk is only transmitted once there is no"""
        """ entry with plaintextwal.bacap_stream=plaintextwal.after_stream left.""",
    )
    bacap_stream: uuid.UUID = Field(index=True,)
    conversation_id: int = Field(foreign_key="conversation.id", index=True,)
    bacap_payload: bytes  # output of models.SendOperation.serialize()
    indirection: uuid.UUID = Field(foreign_key="readcapwal.id", index=True, default=None, nullable=True, description="when we are serializing a models.SendOperation that spans multiple boxes we want to put a ReadCapWAL entry in bacap_payload, but we can't generate those without clientd. setting this field indicates that we need such a ReadCapWAL to be populated first.")
    

    @classmethod
    def find_resendable(cls, resend_queue: "Set[uuid.UUID]") -> sa.sql.selectable.Select:
        # We can start resending in these cases:
        # - after_stream IS NONE AND after_id IS NONE
        # - after_stream IS NONE AND after_id IN (sentIDs)
        # - after_stream IN (completedStreams) AND after_id IS NONE
        # - after_stream IN (completedStreams) AND after_id IN (sentIDs)
        # ... but only if we aren't already resending msgs from this stream (resend_queue).
        # if indirection= IS NONE OR indirection EXISTS in ReadCapWAL.read_cap IS NOT NONE is populated

        # alternative:
        """
        @sqlite> SELECT anon_1.id, anon_1.after_id, anon_1.after_stream, anon_1.bacap_stream, anon_1.conversation_id, anon_1.bacap_payload, anon_1.rownum  FROM (SELECT plaintextwal.id AS id, plaintextwal.after_id AS after_id, plaintextwal.after_stream AS after_stream, plaintextwal.bacap_stream AS bacap_stream, plaintextwal.conversation_id AS conversation_id, plaintextwal.bacap_payload AS bacap_payload, row_number() OVER (PARTITION BY plaintextwal.bacap_stream) AS rownum  FROM plaintextwal LEFT JOIN sentlog ON sentlog.id IN (plaintextwal.after_stream, plaintextwal.after_id) WHERE (plaintextwal.id NOT IN (1,2)) AND (plaintextwal.after_id IS NULL OR plaintextwal.after_id=sentlog.id) AND (plaintextwal.after_stream IS NULL OR plaintextwal.after_stream=sentlog.id)) AS anon_1 where anon_1.rownum = 1;
        """
        sent_cte = sa.select(sa.select(SentLog.id).cte('sent_cte'))  # Successfully sent messages
        mixwal_bacap_cte = sa.select(sa.select(MixWAL.bacap_stream).cte('mixwal_bacap_cte'))
        populated_read_cap_cte = sa.select(sa.select(ReadCapWAL.id).where(ReadCapWAL.read_cap != None).cte("populated_read_cap_cte"))
        populated_write_cap_cte = sa.select(sa.select(WriteCapWAL.id).where(WriteCapWAL.write_cap != None).cte("populated_write_cap_cte"))
        # Aliased copy of the table for the after_stream gate's correlated
        # NOT EXISTS subquery. The gate fires when the referenced
        # bacap_stream has no remaining PWALs, which (since mark_sent
        # deletes the PWAL row in the same transaction it writes SentLog)
        # is the correct end-of-stream condition. The previous attempt
        # compared after_stream (a bacap_stream UUID) against SentLog.id
        # (a PWAL UUID), two disjoint identifier spaces, so the gate was
        # permanently closed for every indirection PWAL of every multi-box
        # send. The release also implicitly handles the "all PWALs for
        # this stream are still pending" case because some chunk's
        # bacap_stream IS the target after_stream.
        from sqlalchemy.orm import aliased as _aliased
        _other_pwal = _aliased(PlaintextWAL)
        # instead of a cte that selects it all we may want to us after_id/after_stream directly
        row = sa.func.row_number().over(partition_by=PlaintextWAL.bacap_stream).label("rownum")
        all_elig= (
            sa.select(PlaintextWAL, row)
            .where(
                PlaintextWAL.bacap_stream.not_in(mixwal_bacap_cte),  # one msg per BACAP stream at a time
            )
            .where(
                sa.and_(
                    # __resend_queue holds bacap_stream UUIDs (not PWAL ids),
                    # so the filter column has to match. The prior
                    # PlaintextWAL.id.not_in(resend_queue) was a silent no-op
                    # because the two UUID spaces essentially never overlap.
                    PlaintextWAL.bacap_stream.not_in(resend_queue),
                    sa.or_(PlaintextWAL.after_id.is_(None),
                        PlaintextWAL.after_id.in_(sent_cte)
                        ),
                    sa.or_(
                        PlaintextWAL.after_stream.is_(None),
                        # No PWAL remains on the referenced bacap_stream.
                        sa.not_(sa.exists().where(
                            _other_pwal.bacap_stream == PlaintextWAL.after_stream
                        )),
                    ),
                )
            )
            .where(
                sa.or_(
                    # Not an b'I'ndirection:
                    PlaintextWAL.indirection.is_(None),
                    # We can synthesize the b'I'ndirection because we now have the read cap:
                    PlaintextWAL.indirection.in_(populated_read_cap_cte)  # If it has been provisioned
                )
            )
            .where(
                PlaintextWAL.bacap_stream.in_(populated_write_cap_cte)  # if not, it's still waiting for provisioning
            )
        ).subquery()
        # return at most one resendable per bacap_stream:
        # TODO we need to avoid multiple things being committed to use the same index,
        # TODO and selecting the first unsent for each BACAP stream means we'll eventually put all of them there.
        # TODO need to rethink the logic here; should probably not have more than one thing serialized per bacap stream at time.
        # TODO also dubious to have the insert into MixWAL be in a separate transaction from where we mark the PlaintextWAL as "spent"
        # TODO - ie, shouldn't have __resend_queue be a python variable, but rather a database concept.
        return sa.select(all_elig).filter(all_elig.c.rownum == 1)

class ReceivedPiece(SQLModel, table=True):
    """Pieces for reassembling a SendOperation into ConversationLog entries.
    SendOperation.serialize() emits PlaintextWAL entries that get put in Pigeonhole boxes.
    When we receive Pigeonhole boxes they are kept as ChatReassembly pieces until the original
    GroupChatMessage can be reconstructed.
    """
    # Composite PK:
    read_cap : uuid.UUID = Field(primary_key=True, foreign_key="readcapwal.id", index=True, description="RCW this RP came from.")
    bacap_index : bytes = Field(primary_key=True,min_length=8,max_length = 8) # the 8-byte current_message_index for the Pigeonhole box. SQLite doesn't support 64bit unsigned ints, so we can't use a numeric type here.
    
    chunk_type: bytes = Field(min_length=1,max_length=1) # Whether the piece is a b'F'inal or b'C'ontinued or b'I'ndirection piece.
    chunk: bytes = Field(description="Received payload, excluding the chunk_type")
    # When we have unlocked a chunk_type=b"F" we need to look for the parent stream so we know where to insert it.
    # We should also mark sess.get(ReadCapWAL, rp.read_cap).active=False unless it's referred to by a ConversationPeer directly.

class ConversationPeerLink(SQLModel, table=True):
    conversation_peer_id: int | None = Field(default=None, foreign_key="conversationpeer.id", primary_key=True)
    conversation_id: int | None = Field(default=None, foreign_key="conversation.id", primary_key=True)

class ConversationPeer(SQLModel, table=True):
    #id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    id: int = Field(default=None, primary_key=True)
    name: str = Field(index=True, min_length=1, max_length=30)
    read_cap_id: uuid.UUID = Field(foreign_key="readcapwal.id", index=True, description="point to the read cap we need to use to read this peer")
    active: bool = Field(default=True, description="Do we try to read this?")
    conversation : "Conversation" = Relationship(back_populates="peers", link_model=ConversationPeerLink, sa_relationship_kwargs={"lazy":"selectin"})

class Conversation(SQLModel, table=True):
    #id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # TODO see conversationlog where we want to use uuid instead of int for privacy reasons
    id: int = Field(default=None, primary_key=True)

    name: str = Field(index=True, min_length=1, max_length=50)
    """Name of the conversation"""

    # TODO we probably want some more metadata here
    # like: message expiry deadlines
    own_peer_id : int = Field(foreign_key="conversationpeer.id", index=True, )
    own_peer : ConversationPeer = Relationship(sa_relationship_kwargs={"lazy":"selectin"})

    #last_read : int = Field(foreign_key="conversationlog.id",)
    # TODO what's the fastest way to compute the number of read messages?
    # can we do it without a full table join? how do we achieve the least amount of mutation?

    # each conversation has one BACAP write cap:
    write_cap: uuid.UUID = Field(default_factory=uuid.uuid4)
    # TODO: the write cap mutates because the index gets bumped.
    # TODO: should we have a separate table for that so we don't need to mutate the Conversation
    # TODO: over and over, or should we just UPDATE?

    # and a number of BACAP read caps
    peers: list[ConversationPeer] = Relationship(back_populates="conversation", link_model=ConversationPeerLink, sa_relationship_kwargs={"lazy":"selectin"})
    log: list["ConversationLog"] = Relationship(back_populates="conversation", sa_relationship_kwargs={"lazy":"selectin"})
    #sa_relationship=RelationshipProperty("ConversationLog", foreign_keys=["fk_conversationlog_id_conversation_id"])

    first_unread: int = Field(nullable=True, default=None, description="pointer to latest read ConversationLog entry")
    #first_unread: uuid.UUID = Field(foreign_key="conversationlog.id", nullable=True, index=False, description="pointer to latest read ConversationLog entry")
    # to keep track of the read state "split buffer"

    voucher_used: bool = Field(
        default=False, nullable=False,
        sa_column_kwargs={"server_default": sa.text("0")},
        description="a Contact Voucher handshake completed successfully for this conversation",
    )

class ConversationLog(SQLModel, table=True):
    """CBOR messages in a conversation.

    Not all of these are displayed in the UI.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # we use a UUID here so as not to betray the relative receival order
    # of messages from different conversations. the SQLite rows probably still betray that,
    # but at least using UUID here means we could rewrite the database every so often.

    __table_args__ = (
        UniqueConstraint('conversation_id', 'conversation_order', name='uniq_conv_id_and_order'),
        # the composite unique constraint here ensures all conversation_order are distinct (per conversation_id)
    )

    conversation_id: int = Field(foreign_key="conversation.id", index=True, )
    conversation : Conversation = Relationship(back_populates="log", sa_relationship_kwargs={"lazy":"selectin"})

    conversation_peer_id : int = Field(foreign_key="conversationpeer.id", index=True)
    conversation_peer : ConversationPeer = Relationship(sa_relationship_kwargs={"lazy":"selectin"})

    # TODO: should we store a cached "type" here?

    conversation_order: int = Field(index=True)
    # This is the relative order of messages within a Conversation
    # TODO: that will show messages in the order they are received, not in logical order
    # TODO: but we don't have a good way to establish a logical clock for a conversation with
    # TODO: multiple peers.

    # this thing here needs to turn into
    # conversation_log.setModel(cl)
    # conversation_log.setRootIndex(cl)

    # TODO should we keep track of whether this has been sent? we will have a corresponding Sentlog
    # TODO entry, but we will also want to expunge the Sentlog periodically to conserve disk space.
    # TODO could maybe just have a boolean. But for display purposes we'll want this information
    # TODO indefinitey; for our own messages.

    # TODO: envelope_hash: bytes - do we need this?
    payload: bytes  # This contains binary CBOR

    network_status: int = Field(default=0) # 0: received; 1:pending; 2: fully sent
    outgoing_pwal: uuid.UUID | None = Field(
        default=None,
        foreign_key="plaintextwal.id",
        index=True,
        description="""the PlaintextWAL entry for the final message that marks this sent""",
    )
    @classmethod
    def append_from(cls, conversation_peer: ConversationPeer, payload) -> "ConversationLog":
        """Add payload to the end end of conversation_peer. It is the caller's responsibility to add
        the new ConversationLog instance to a Session and commit it; this constructor merely creates
        the Python object.
        """
        return cls(
            conversation_id=conversation_peer.conversation.id,
            conversation_peer=conversation_peer,
            payload=payload,
            conversation_order=next_conversation_order(conversation_peer.conversation.id),
        )


class TallyState(SQLModel, table=True):
    """The convergent state of one tally survey, as a single CRDT blob.

    One row per survey, overwritten on every mutation (the blob is the whole
    pycrdt ``Doc`` as one update). On startup the rows are loaded back into
    in-memory Docs so surveys survive a restart and the sync path has prior
    state to diff against. The survey id is the BACAP-derived identifier minted
    at creation; ``conversation_id`` ties the survey to the group it lives in.
    """
    survey_id: bytes = Field(primary_key=True, min_length=1)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    doc_state: bytes

