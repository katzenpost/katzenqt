import annotated_types
from typing_extensions import Annotated
from pydantic import Field, BaseModel, SecretBytes, SecretStr, Strict, field_serializer, model_validator
import cbor2
from enum import Enum
import uuid
import secrets
import io
from . import persistent
import hashlib
from base64 import b64encode, b64decode
from typing import List
from pathlib import Path

# Note: ``ConversationUIState`` used to live here but its Qt-typed fields
# (ConversationLogModel, QStandardItem, QQmlPropertyMap) forced every
# importer of this module — including the headless integration runner
# and pytest collection — to load PySide6 and the Qt runtime libraries.
# It now lives in ``katzenqt.qt_models``; import it from there if you
# need it.

class GroupChatTEXT(BaseModel):
    model_config = {
        'validate_assignment': True }
    payload: str

class GroupChatPleaseAdd(BaseModel):
    """https://katzenpost.network/docs/specs/group_chat.html - Invitation"""
    model_config = {
        'validate_assignment': True
    }
    display_name: str = Field(min_length=1, max_length=30)
    read_cap: bytes = Field(min_length=136, max_length=136)
    def to_cbor(self) -> bytes:
        return cbor2.dumps(self.model_dump(exclude_none=True))
    def to_human_readable(self) -> str:
        return b64encode(self.to_cbor()).replace(b'\n',b'').strip().decode()
    @classmethod
    def from_human_readable(cls, text:str) -> "GroupChatPleaseAdd":
        return cls(**cbor2.loads(b64decode(text.strip().encode()))) # TODO this can obv fail

class GroupChatReplyWho(BaseModel):
    model_config = {
        'validate_assignment': True
    }
    please_adds : list[GroupChatPleaseAdd] = Field()
    def to_cbor(self) -> bytes:
        return cbor2.dumps(self.model_dump(exclude_none=True))
    @classmethod
    def from_cbor(cls, data: bytes) -> "GroupChatReplyWho":
        return cls(**cbor2.loads(data))
    def membership_hash(self) -> bytes:
        return hashlib.blake2b(self.to_cbor(), digest_size=32).digest()
    # the conversation hash should be available
    

class GroupChatTypeEnum(Enum):
    TEXT = 0
    INTRODUCTION = 1
    FILE_UPLOAD = 2
    WHO = 3
    REPLY_WHO = 4
    # The tally protocol's message family. A survey is created, voted upon, and
    # closed; the two SYNC kinds carry the CRDT catch-up exchange. See
    # ``katzenqt.tally`` and ``katzenqt.conversation_handlers``.
    TALLY_CREATE = 5
    TALLY_VOTE = 6
    TALLY_CLOSE = 7
    TALLY_SYNC_REQ = 8
    TALLY_SYNC_RESP = 9

class SendOperation(BaseModel):
    messages: "List[GroupChatMessage]"
    bacap_stream: uuid.UUID

    #@validator("messages")
    #def validate(cls, v: "List[GroupChatMessage]") -> "List[GroupChatMessage]":
    #    if not v:
    #        raise ValueError("must have at least one message")
    #    return v

    def serialize(self, chunk_size: int, conversation_id: int) -> "Tuple[List[uuid.UUID], List[persistent.PlaintextWAL]]":
        """Produce data to go into PlaintextWAL.

        TODO chunk_size = BoxPayloadLength = 1556

        This produces the BACAP payloads to insert.
        The first element of the returned tuple is a list of new UUIDs of BACAP WriteCap(s) that needs to be generated.
        The second element is the messages that need to be sent to the mixnet.

        TODO: We probably want to make several nested BACAP streams so readers can skip large files without losing messages.
        """
        if chunk_size <= 1:
            raise Exception("can't serialize if max payload size is < 2 bytes")
        if not self.messages:
            return [], [] # There's nothing to send
        buf = io.BytesIO()
        for msg in self.messages:
            buf.write(msg.to_cbor())
        total_size = buf.tell()
        buf.seek(0)
        if total_size + 1 <= chunk_size:
            # message fits in a single chunk
            return [], [
                persistent.PlaintextWAL(
                    id=uuid.uuid4(),
                    after_id=None,
                    after_stream=None,
                    bacap_stream=self.bacap_stream,
                    conversation_id=conversation_id,
                    bacap_payload=b'F' + buf.read()
                )
            ]
        # Else we need to write it into a sub-stream.
        # We have a problem here which is that we need the daemon running in order to produce write caps.
        agg = []
        prev_id : uuid.UUID | None = None
        agg_bacap_stream = uuid.UUID(bytes=secrets.token_bytes(16))
        for off in range(0, total_size - (chunk_size-1), chunk_size - 1):
            this_id = uuid.uuid4()
            agg.append(persistent.PlaintextWAL(
                id=this_id,
                after_id=prev_id,
                bacap_stream=agg_bacap_stream,
                conversation_id=conversation_id,
                bacap_payload=b'C' + buf.read(chunk_size - 1)
            ))
            prev_id = this_id
        this_id = uuid.UUID(bytes=secrets.token_bytes(16)) # uuid.uuid4() should also work
        agg.append(persistent.PlaintextWAL(
                id=this_id,
                after_id=prev_id,
                bacap_stream=agg_bacap_stream,
                conversation_id=conversation_id,
                bacap_payload=b'F' + buf.read(chunk_size - 1)
        ))

        buf_tell = buf.tell()
        assert buf_tell == buf.seek(0, 2)  # assert that we were at the end

        # Put the release in the original bacap stream:
        # 1. We need a ReadCapWal that points to the `agg_bacap_stream`:
        rcw = persistent.ReadCapWAL(id=uuid.uuid4(), write_cap_id=agg_bacap_stream, active=False)
        agg.append(rcw)
        # 2. the b'I'ndirection entry needs to point to rcw.id, so the read
        #    cap can be filled once we have received it from clientd, and
        #    only then can this operation be churned out as a WriteCap. The
        #    primary key is assigned up front so callers (notably the
        #    headless runner) can capture it before the session commit and
        #    later watch SentLog for it.
        agg.append(
            persistent.PlaintextWAL(
                id=uuid.uuid4(),
                after_id=None,
                after_stream=agg_bacap_stream,
                bacap_stream=self.bacap_stream,
                conversation_id=conversation_id,
                bacap_payload=b'', # b'I' + agg_stream_read_cap,
                indirection=rcw.id,
            )
        )
        return [agg_bacap_stream], agg

class GroupChatFileUpload(BaseModel):
    model_config = {'validate_assignment': True}
    payload : bytes
    filetype: str # "image, sound, arbitrary"
    basename: str

    @classmethod
    def from_path(cls, path: str | Path) -> "GroupChatFileUpload":
        file_path = Path(path)
        # Voice notes reuse the generic file-upload transport, so the filetype
        # tag is the only signal the renderer needs to switch to audio UI.
        filetype = "audio/opus" if file_path.suffix.lower() == ".opus" else "arbitrary"
        return cls(
            payload=file_path.read_bytes(),
            filetype=filetype,
            basename=file_path.name,
        )

class GroupChatTally(BaseModel):
    """The payload carried by every tally message. Which fields are populated
    follows from the message's ``msg_type``:

    * ``TALLY_CREATE`` / ``TALLY_SYNC_RESP``: ``crdt`` holds an opaque CRDT
      update (the full initial state, or a diff since a state vector).
    * ``TALLY_VOTE``: ``choice`` holds the sender's ``slot_id -> availability``
      selection. The receiver writes it under the *authenticated* sender's key,
      never an id taken from the payload.
    * ``TALLY_SYNC_REQ``: ``crdt`` holds the requester's state vector.
    * ``TALLY_CLOSE``: neither; ``survey_id`` and ``version`` suffice.
    """
    model_config = {'validate_assignment': True}
    survey_id: bytes = Field(min_length=1)
    version: int = Field(default=0, ge=0)
    choice: dict[str, str] | None = Field(default=None)
    crdt: bytes | None = Field(default=None)

class GroupChatMessage(BaseModel):
    """
    """
    model_config = {'validate_assignment': True}
    version: int = Field(ge=0,)
    membership_hash : "Annotated[bytes, Strict(), annotated_types.Len(32, 32),]"

    # The message's type, made explicit so a conversation handler can route by
    # it rather than guessing from which optional field is set. Serialised as
    # its integer value (cbor2 cannot encode a bare Enum); decoded back to the
    # enum. Absent on payloads written before this field existed, in which case
    # it is inferred from the populated field below.
    msg_type: GroupChatTypeEnum = Field(default=GroupChatTypeEnum.TEXT)

    text: str | None = Field(default=None)
    introduction: GroupChatPleaseAdd | None = Field(default=None)
    file_upload: GroupChatFileUpload | None = Field(default=None)
    who: str | None = Field(default=None)
    reply_who: str | None = Field(default=None)
    tally: GroupChatTally | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _infer_msg_type(cls, data):
        """Fill ``msg_type`` from the populated field when it is absent, so a
        legacy payload decodes to the right type."""
        if isinstance(data, dict) and data.get("msg_type") is None:
            data = dict(data)
            for field, kind in (
                ("introduction", GroupChatTypeEnum.INTRODUCTION),
                ("file_upload", GroupChatTypeEnum.FILE_UPLOAD),
                ("who", GroupChatTypeEnum.WHO),
                ("reply_who", GroupChatTypeEnum.REPLY_WHO),
                ("tally", None),
            ):
                if field == "tally":
                    continue  # tally messages always carry an explicit msg_type
                if data.get(field) is not None:
                    data["msg_type"] = kind.value
                    break
            else:
                data["msg_type"] = GroupChatTypeEnum.TEXT.value
        return data

    @field_serializer("msg_type")
    def _serialize_msg_type(self, value: GroupChatTypeEnum, _info):
        return value.value

    def to_cbor(self):
        """A group chat message consists of one CBOR messages potentially
        serialized over one or more BACAP boxes.

        - When it consists of multiple, we need to write a "subchain"
        - When it consists of one, we can probably write it directly.

        - The exception is that when we need to send more than one message at the same time,
        """
        return cbor2.dumps(self.model_dump(exclude_none=True))

    @classmethod
    def from_cbor(cls, cbor_bytes:bytes):
        return cls(**cbor2.loads(cbor_bytes)) # TODO not at all what we want but here we go
        dec = cbor2.CBORDecoder(fp=io.BytesIO(cbor_bytes))
        try:
            dic = dec.decode()
        except cbor2.CBORDecodeEOF:
            raise
        assert 3 != len(list(dic.keys()))
        gcm = cls(**dic)
        return gcm, d.fp.tell()

def unserialize(chunks) -> "GroupChatMessage | None":
    """Reassemble a serialised ``SendOperation`` chain into a
    :class:`GroupChatMessage`.

    ``chunks`` is an iterable of ``(chunk_type, chunk_bytes)`` tuples
    ordered by BACAP index. ``chunk_type`` is the single-byte framing
    marker emitted by :meth:`SendOperation.serialize`:

    * ``b'C'`` — continuation; carries an interior slice of the
      CBOR-encoded message,
    * ``b'F'`` — final; carries the last slice, terminating the chain,
    * ``b'I'`` — indirection; reserved for the network-layer coalescer
      which follows the embedded read cap and feeds the substream's
      chunks back in. The data layer refuses to treat it as payload.

    Returns the decoded :class:`GroupChatMessage` if the chain is
    contiguous and ends in ``b'F'``. Returns ``None`` when the chain
    does not yet end in ``b'F'`` (more chunks still expected, or an
    empty chain). Raises :class:`ValueError` on framing violations:
    unknown chunk types, ``b'I'`` chunks, or a ``b'F'`` chunk that is
    not the last in the sequence.

    No assumption is made about whether the chunks originated from the
    original ``bacap_stream`` or from an indirection substream; the
    caller has already classified them by the time they reach here.
    """
    parts = []
    chunk_list = list(chunks)
    if not chunk_list:
        return None
    for i, (kind, data) in enumerate(chunk_list):
        if kind == b"C":
            if i == len(chunk_list) - 1:
                return None  # chain still open, no terminating 'F'
            parts.append(data)
        elif kind == b"F":
            if i != len(chunk_list) - 1:
                raise ValueError(
                    f"'F' chunk at index {i} but {len(chunk_list) - 1 - i} "
                    "chunk(s) follow"
                )
            parts.append(data)
            return GroupChatMessage.from_cbor(b"".join(parts))
        elif kind == b"I":
            raise ValueError(
                "indirection ('I') framing must be resolved by the network "
                "coalescer before reaching models.unserialize"
            )
        else:
            raise ValueError(f"unknown chunk type: {kind!r}")
    return None


# ConversationUIState moved to katzenqt.qt_models — see banner near the
# top of this file for rationale.
