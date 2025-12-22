import sqlalchemy as sa
from sqlalchemy.orm import declarative_base
#from pydantic import BaseModel, ConfigDict, Field
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
import struct
from typing import TYPE_CHECKING
from katzen_util import create_task
if TYPE_CHECKING:
    from typing import AsyncContextManager
    import sqlmodel
from alembic import context
import os



_alembic_cfg = alembic.config.Config(Path(__file__).parent.parent / "alembic.ini")

xdg_data_home = (Path().home()/".local"/"share")
xdg_data_home.mkdir(parents=True,exist_ok=True)
app_data = xdg_data_home / "katzenqt"
app_data.mkdir(exist_ok=True, mode=0o700)

if not (state_file := os.getenv("KQT_STATE", "")):
    state_file = "katzen"
state_file += ".sqlite3"
state_file = app_data / state_file
_sql_url = f"sqlite+aiosqlite:///{ state_file }"
print(_sql_url)
_engine = create_async_engine(_sql_url, echo=True, future=True, pool_size=1000)
_engine_sync = create_engine(_sql_url.replace('+aiosqlite://','://'), echo=True, pool_size=1000)


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
    """Opens a sqlmodel.ext.asyncio.session.AsyncSession"""
    async with AsyncSession(_engine) as session:
        yield session

def init_and_migrate():
    """Initialize database and migrates application schema.

    This MUST be called on application startup.
    """
    alembic.command.upgrade(_alembic_cfg, "head")

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
    Stores WriteChannelReply from ThinClient.write_channel_message
    for resending to the courier.
    We need to feed all of these into ThinClient.send_message

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
    destination: bytes  # courier this was supposed to be sent to
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
    read_cap: bytes | None = Field(None, min_length=168, max_length=168)
    next_index: bytes | None = Field(None, min_length=104, max_length=104)
    @classmethod
    async def get_by_bacap_stream(cls, stream: uuid.UUID):
        return (await sess.exec(select(cls).where(id=stream))).one()

class WriteCapWAL(SQLModel, table=True):
    id: uuid.UUID = Field(primary_key=True)
    write_cap: bytes | None = Field(None, min_length=136, max_length=136)
    next_index: bytes | None = Field(None, min_length=104, max_length=104)
    @classmethod
    def get_by_bacap_uuid(cls, uuid):
        # from typing import ClassVar
        # select_by_bacap_uuid: ClassVar[sa.select()] = sa.select(cls).where(cls.id==uuid)
        return sa.select(cls).where(cls.id==uuid)

class SentLog(SQLModel, table=True):
    id: uuid.UUID = Field(primary_key=True)  # previously the UUID assigned in MixWAL
    @classmethod
    async def mark_sent(cls, mw:MixWAL, resend_queue) -> int:
        """Add SentLog entry, delete corresponding MixWAL and PlaintextWAL entries.
        Also bump index so we don't just keep writing to the same index??
        TODO: cleanup WriteCapWAL/ReadCapWAL entries when we are done with them (not urgent, but eventually)

        Returns the Conversation.id for the MixWAL entry so UI can be updated.
        """
        conversation_id = None
        with Session(_engine_sync) as sess:
            pwal = sess.get(PlaintextWAL, mw.plaintextwal)
            if not pwal:
                print("pwal lookup failed for mw.plaintextwal", mw.is_read, mw)
                # this is not great; if mw.is_read==False then we really ought to have a corresponding plaintextwal.
                # either we put the wrong id here; or something went wrong making the entry; or we have already mark_sent something
                # for this plaintextwal entry. It seems most likely that we would end up here if we have resent the write
                # and only later gotten the ACK.
                real_next, our_next = struct.unpack('<2Q', sess.get(WriteCapWAL, mw.bacap_stream).next_index[:8] + mw.next_message_index[:8])
                if real_next >= our_next:
                    print(f"in mark_sent, but db next {real_next} >= {our_next} we already advanced from idx", mw.current_message_index[:8].hex())
                    resend_queue.discard(mw.bacap_stream)  # TODO not sure if we still use this?
                    return
                import pdb; pdb.set_trace()
            sess.add(cls(id=pwal.id))  # SentLog entry for the pwal id
            wcw = sess.get(WriteCapWAL, mw.bacap_stream)
            print("updating wcw: ", struct.unpack('<2Q', wcw.next_index[:8] + mw.next_message_index[:8]))
            wcw.next_index = mw.next_message_index
            if pwal.bacap_payload[:1] in (b'F',b'I'):
                # This is either:
                #   I: an Indirection release pointing to something else
                #   F: a Final message
                # If it's at a top level, we would have a local ConversationLog entry already,
                # and we can update that to reflect that message has been sent.
                if convlog := sess.exec(select(ConversationLog).where(ConversationLog.outgoing_pwal == pwal.id)).first():
                    conversation_id = convlog.conversation_id
                    convlog.network_status = 2
                    sess.add(convlog)
            sess.add(wcw)
            if mw.bacap_stream != pwal.bacap_stream:
                logger.error("mw.bacap_stream doesn't match pwal.bacap_stream")
            sess.delete(sess.get(MixWAL, mw.id))
            sess.delete(pwal)
            # TODO maybe update ConversationLog entry if we start tracking sent msgs in the UI
            sess.commit()
            resend_queue.discard(pwal.bacap_stream)  # TODO not sure if we still use this?
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
        # instead of a cte that selects it all we may want to us after_id/after_stream directly
        row = sa.func.row_number().over(partition_by=PlaintextWAL.bacap_stream).label("rownum")
        all_elig= (
            sa.select(PlaintextWAL, row)
            .where(
                PlaintextWAL.bacap_stream.not_in(mixwal_bacap_cte),  # one msg per BACAP stream at a time
            )
            .where(
                sa.and_(
                    PlaintextWAL.id.not_in(resend_queue),
                    sa.or_(PlaintextWAL.after_id.is_(None),
                        PlaintextWAL.after_id.in_(sent_cte)
                        ),
                    sa.or_(PlaintextWAL.after_stream.is_(None),
                        PlaintextWAL.after_stream.in_(sent_cte)),
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
            conversation_order=(
                select(count())
                .select_from(cls)
                .where(cls.conversation_id == conversation_peer.conversation.id)
                .scalar_subquery()),
        )

