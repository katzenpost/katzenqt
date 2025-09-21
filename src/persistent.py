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
import aiosqlite # https://pypi.org/project/aiosqlite/
from typing import TYPE_CHECKING

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
_engine = create_async_engine(_sql_url, echo=True, future=True)
_engine_sync = create_engine(_sql_url.replace('+aiosqlite://','://'), echo=True)


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
    id: int = Field(default=None, primary_key=True)
    bacap_stream: uuid.UUID = Field(unique=True) # There can only be one active write per stream
    # it can't be a foreign_key because we track ReadCapWAL+WriteCapWAL separately.
    envelope_hash: bytes = Field(unique=True) # How do we compute this?
    destination: bytes  # courier this was supposed to be sent to
    encrypted_payload: bytes
    next_message_index: bytes
    is_read : bool
    @classmethod
    def get_new(cls, resend_queue:"Set[uuid.UUID]"):
        return select(cls).where(cls.bacap_stream.not_in(resend_queue))
    @classmethod
    async def resend_queue_from_disk(cls) -> "Set[uuid.UUID]":
        __resend_queue = set()
        async with asession() as sess:
            for stream in await sess.exec(select(cls.bacap_stream)):
                print("RESEND QUEUE FROM DISK:", stream)
                __resend_queue.add(stream)
        return __resend_queue

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
    id: uuid.UUID = Field(primary_key=True)

class PlaintextWAL(SQLModel, table=True):
    """Plaintext chunks of (bacap_payload) to insert into (bacap_stream)."""
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
    bacap_payload: bytes

    @classmethod
    def find_resendable(cls, resend_queue: "Set[uuid.UUID]") -> sa.sql.selectable.Select:
        # We can start resending in these cases:
        # - after_stream IS NONE AND after_id IS NONE
        # - after_stream IS NONE AND after_id IN (sentIDs)
        # - after_stream IN (completedStreams) AND after_id IS NONE
        # - after_stream IN (completedStreams) AND after_id IN (sentIDs)
        # ... but only if we aren't already resending them (resend_queue).

        # alternative:
        """
        @sqlite> SELECT anon_1.id, anon_1.after_id, anon_1.after_stream, anon_1.bacap_stream, anon_1.conversation_id, anon_1.bacap_payload, anon_1.rownum  FROM (SELECT plaintextwal.id AS id, plaintextwal.after_id AS after_id, plaintextwal.after_stream AS after_stream, plaintextwal.bacap_stream AS bacap_stream, plaintextwal.conversation_id AS conversation_id, plaintextwal.bacap_payload AS bacap_payload, row_number() OVER (PARTITION BY plaintextwal.bacap_stream) AS rownum  FROM plaintextwal LEFT JOIN sentlog ON sentlog.id IN (plaintextwal.after_stream, plaintextwal.after_id) WHERE (plaintextwal.id NOT IN (1,2)) AND (plaintextwal.after_id IS NULL OR plaintextwal.after_id=sentlog.id) AND (plaintextwal.after_stream IS NULL OR plaintextwal.after_stream=sentlog.id)) AS anon_1 where anon_1.rownum = 1;
        """
        sent_cte = sa.select(sa.select(SentLog.id).cte('sent_cte'))
        # instead of a cte that selects it all we may want to us after_id/after_stream directly
        row = sa.func.row_number().over(partition_by=PlaintextWAL.bacap_stream).label("rownum")
        all_elig= (
            sa.select(PlaintextWAL, row)
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
        ).subquery()
        # return at most one resendable per bacap_stream:
        return sa.select(all_elig).filter(all_elig.c.rownum == 1)

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

    # TODO: envelope_hash: bytes - do we need this?
    payload: bytes  # This contains binary CBOR
