import sqlalchemy as sa
from sqlalchemy.orm import declarative_base
#from pydantic import BaseModel, ConfigDict, Field
from pathlib import Path
import alembic.config
import alembic.command
import uuid
from contextlib import asynccontextmanager
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine, UniqueConstraint
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
import aiosqlite # https://pypi.org/project/aiosqlite/
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import AsyncContextManager
    import sqlmodel

_alembic_cfg = alembic.config.Config(Path(__file__).parent / "alembic.ini")
_sql_url = _alembic_cfg.file_config._sections['alembic']['sqlalchemy.url']  # TODO this should reside in ~/.local/state/ or similar xdg path
_engine = create_async_engine(_sql_url, echo=True, future=True)
_engine_sync = create_engine("sqlite:///katzen.sqlite3", echo=True)


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

async def make_conn():
    """TODO placeholder"""
    async with asession() as session:
        f = ConversationPeer(name="contact1", read_cap=b"hey")
        #import pdb;pdb.set_trace()
        session.add(f)
        await session.commit()
        pass
    pass

def id_field(table_name: str):
    sequence = sa.Sequence(f"{table_name}_id_seq")
    return Field(
        default=None,
        primary_key=True,
        sa_column_args=[sequence],
        sa_column_kwargs={"server_default": sequence.next_value()},
    )

class MixWAL(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    envelope_hash: bytes # TODO this should be unique
    # TODO: this should store what comes out of ThinClient.write_channel_message
    # TODO: so that we can ThinClient.send_message it
    # destination : bytes # courier this was supposed to be sent to
    # payload: bytes

class ConversationPeerLink(SQLModel, table=True):
    conversation_peer_id: int | None = Field(default=None, foreign_key="conversationpeer.id", primary_key=True)
    conversation_id: int | None = Field(default=None, foreign_key="conversation.id", primary_key=True)

class ConversationPeer(SQLModel, table=True):
    #id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    id: int = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    read_cap: bytes
    active: bool = Field(default=True)
    conversation : "Conversation" = Relationship(back_populates="peers", link_model=ConversationPeerLink, sa_relationship_kwargs={"lazy":"selectin"})

class Conversation(SQLModel, table=True):
    #id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # TODO see conversationlog where we want to use uuid instead of int for privacy reasons
    id: int = Field(default=None, primary_key=True)

    name: str = Field(index=True)
    """Name of the conversation"""

    # TODO we probably want some more metadata here
    # like: message expiry deadlines
    own_peer_id : int = Field(foreign_key="conversationpeer.id", index=True, )
    own_peer : ConversationPeer = Relationship(sa_relationship_kwargs={"lazy":"selectin"})

    #last_read : int = Field(foreign_key="conversationlog.id",)
    # TODO what's the fastest way to compute the number of read messages?
    # can we do it without a full table join? how do we achieve the least amount of mutation?

    # each conversation has one BACAP write cap:
    write_cap: bytes
    # TODO: the write cap mutates because the index gets bumped.
    # TODO: should we have a separate table for that so we don't need to mutate the Conversation
    # TODO: over and over, or should we just UPDATE?

    # and a number of BACAP read caps
    peers: list[ConversationPeer] = Relationship(back_populates="conversation", link_model=ConversationPeerLink, sa_relationship_kwargs={"lazy":"selectin"})
    log: list["ConversationLog"] = Relationship(back_populates="conversation", sa_relationship_kwargs={"lazy":"selectin"})

    # first_unread: nullable ForeignKey to conversation_order
    # to keep track of the read state "split buffer"

class ConversationLog(SQLModel, table=True):
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
