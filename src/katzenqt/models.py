import annotated_types
import asyncio
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
from collections.abc import Iterable
from typing import TYPE_CHECKING, List, Tuple, Union
from pathlib import Path

SerializedRow = Union[persistent.PlaintextWAL, persistent.ReadCapWAL]

if TYPE_CHECKING:
    from pydantic import FieldSerializationInfo

# --- membership hash (GROUP_CHAT_PROTOCOL.md section 6b) ------------------
# The recipe is fixed so that independent implementations compute the same
# 32-byte digest over the same member set.

SUBSTREAM_NAME_PREFIX = ":substream:"

MEMBERSHIP_DOMAIN = b"KP:membership:v1"

MEMBERSHIP_SENTINELS = (b"TODO" * 8, bytes(32))


def is_membership_sentinel(digest: bytes) -> bool:
    """Whether ``digest`` is a 'no membership hash' sentinel accepted
    without comparison during the migration window.

    >>> is_membership_sentinel(b"TODO" * 8)
    True
    >>> is_membership_sentinel(bytes(32))
    True
    >>> is_membership_sentinel(canonical_membership_hash([bytes(136)]))
    False
    """
    return digest in MEMBERSHIP_SENTINELS


def canonical_membership_hash(read_caps: Iterable[bytes]) -> bytes:
    """Order-independent membership hash of a set of member read caps:
    take each cap's 32-byte public-key prefix, dedupe and sort those
    byte-wise, concatenate, and SHA-256 under :data:`MEMBERSHIP_DOMAIN`.

    Hashing the prefix (not the whole cap) keeps the digest stable across
    the index/mutation suffix variants of the same member's read cap — a
    joiner's pre-mutation cap, the salt-mutated cap the group holds, and
    future-only read caps starting at a later index all collapse to one
    member. The caller represents itself as ``write_cap[32:]``.

    >>> key_a = bytes(range(32))
    >>> key_b = bytes(range(32, 64))
    >>> cap_a = key_a + bytes(104)
    >>> cap_b = key_b + bytes(104)
    >>> len(cap_a)
    136
    >>> canonical_membership_hash([cap_a, cap_b]) == canonical_membership_hash(
    ...     [cap_b, cap_a])
    True
    >>> canonical_membership_hash(
    ...     [cap_a, key_a + bytes(103) + bytes([1])],
    ... ) == canonical_membership_hash([cap_a])
    True
    >>> canonical_membership_hash([cap_a]) == canonical_membership_hash(
    ...     [cap_a, cap_b])
    False
    >>> len(canonical_membership_hash([cap_a]))
    32
    """
    digest = hashlib.sha256()
    digest.update(MEMBERSHIP_DOMAIN)
    for key in sorted({cap[:32] for cap in read_caps}):
        digest.update(key)
    return digest.digest()

# Note: ``ConversationUIState`` used to live here but its Qt-typed fields
# (ConversationLogModel, QStandardItem, QQmlPropertyMap) forced every
# importer of this module, including the headless integration runner
# and pytest collection, to load PySide6 and the Qt runtime libraries.
# It now lives in ``katzenqt.qt_models``; import it from there if you
# need it.

MAX_MESSAGE_CHARS = 16 * 1024
_TEXT_TRUNCATION_MARKER = "\n[message truncated]"


def clamp_message_text(text: str) -> str:
    """Clamp ``text`` to :data:`MAX_MESSAGE_CHARS`, appending a short marker
    when it is truncated. Idempotent: because the slice happens before the
    marker is appended, clamping an already-clamped string returns the same
    result, so an ingest-time clamp and a render-time clamp compose without
    stacking markers.

    >>> clamp_message_text("hi")
    'hi'
    >>> clamped = clamp_message_text("a" * (MAX_MESSAGE_CHARS + 500))
    >>> len(clamped) == MAX_MESSAGE_CHARS + len(_TEXT_TRUNCATION_MARKER)
    True
    >>> clamp_message_text(clamped) == clamped
    True
    """
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[:MAX_MESSAGE_CHARS] + _TEXT_TRUNCATION_MARKER


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
        """The CBOR encoding of this invitation.

        >>> add = GroupChatPleaseAdd(display_name="alice", read_cap=bytes(136))
        >>> sorted(cbor2.loads(add.to_cbor()))
        ['display_name', 'read_cap']
        >>> len(cbor2.loads(add.to_cbor())["read_cap"])
        136
        """
        return cbor2.dumps(self.model_dump(exclude_none=True))
    def to_human_readable(self) -> str:
        """The invitation as one unbroken line of base64, safe to paste.

        >>> add = GroupChatPleaseAdd(display_name="alice", read_cap=bytes(136))
        >>> text = add.to_human_readable()
        >>> len(text), chr(10) in text
        (224, False)
        >>> GroupChatPleaseAdd.from_human_readable(text) == add
        True
        """
        return b64encode(self.to_cbor()).replace(b'\n',b'').strip().decode()
    @classmethod
    def from_human_readable(cls, text:str) -> "GroupChatPleaseAdd":
        """Decode an invitation produced by :meth:`to_human_readable`.

        >>> add = GroupChatPleaseAdd(
        ...     display_name="alice", read_cap=bytes(range(32)) + bytes(104),
        ... )
        >>> restored = GroupChatPleaseAdd.from_human_readable(
        ...     " " + add.to_human_readable() + chr(10),
        ... )
        >>> restored.display_name, len(restored.read_cap)
        ('alice', 136)
        >>> restored.read_cap[:4].hex()
        '00010203'
        """
        return cls(**cbor2.loads(b64decode(text.strip().encode()))) # TODO this can obv fail

class GroupChatReplyWho(BaseModel):
    model_config = {
        'validate_assignment': True
    }
    please_adds : list[GroupChatPleaseAdd] = Field()
    def to_cbor(self) -> bytes:
        """The CBOR encoding of the announced member list.

        >>> alice = GroupChatPleaseAdd(display_name="alice", read_cap=bytes(136))
        >>> encoded = GroupChatReplyWho(please_adds=[alice]).to_cbor()
        >>> len(cbor2.loads(encoded)["please_adds"][0]["read_cap"])
        136
        """
        return cbor2.dumps(self.model_dump(exclude_none=True))
    @classmethod
    def from_cbor(cls, data: bytes) -> "GroupChatReplyWho":
        """Decode a member list produced by :meth:`to_cbor`.

        >>> alice = GroupChatPleaseAdd(display_name="alice", read_cap=bytes(136))
        >>> who = GroupChatReplyWho(please_adds=[alice])
        >>> GroupChatReplyWho.from_cbor(who.to_cbor()) == who
        True
        >>> GroupChatReplyWho.from_cbor(who.to_cbor()).please_adds[0].display_name
        'alice'
        """
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

    def serialize(self, chunk_size: int, conversation_id: int) -> "Tuple[List[uuid.UUID], List[SerializedRow]]":
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
        agg: "List[SerializedRow]" = []
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
        #    substream_total_chunks counts the C-chunks plus the
        #    final F chunk, so the reader/GUI can render download progress as
        #    n/total over this substream's ReceivedPiece rows. None means a
        #    legacy (136-byte) I-chunk where the total is unknowable.
        total_c_chunks = len([
            pc for pc in agg
            if isinstance(pc, persistent.PlaintextWAL)
            and pc.bacap_payload[:1] == b'C'
        ])
        rcw = persistent.ReadCapWAL(
            id=uuid.uuid4(), write_cap_id=agg_bacap_stream,
            active=False, substream_total_chunks=total_c_chunks + 1,
        )
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

    async def serialize_async(
        self, *, chunk_size: int, conversation_id: int,
    ) -> "Tuple[List[uuid.UUID], List[SerializedRow]]":
        """Off-loop wrapper around :meth:`serialize`.

        Serialising a large message (CBOR-encoding an attachment, then splitting
        it into BACAP chunks) is a long synchronous stretch. Run it on the
        executor so the calling event loop — Qt or io — stays responsive."""
        return await asyncio.to_thread(
            self.serialize, chunk_size=chunk_size, conversation_id=conversation_id,
        )


class GroupChatFileUpload(BaseModel):
    model_config = {'validate_assignment': True}
    payload : bytes
    filetype: str # "image, sound, arbitrary"
    basename: str

    @classmethod
    def from_path(cls, path: str | Path) -> "GroupChatFileUpload":
        from . import attachment_images

        file_path = Path(path)
        # Voice notes reuse the generic file-upload transport, so the filetype
        # tag is the only signal the renderer needs to switch to audio UI.
        # Images get an image/* tag so the renderer can show a thumbnail;
        # everything else falls back to the generic "arbitrary" marker.
        if file_path.suffix.lower() == ".opus":
            filetype = "audio/opus"
        else:
            filetype = attachment_images.guess_image_filetype(file_path)
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
    def _infer_msg_type(cls, data: object) -> object:
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
                    assert kind is not None
                    data["msg_type"] = kind.value
                    break
            else:
                data["msg_type"] = GroupChatTypeEnum.TEXT.value
        return data

    @field_serializer("msg_type")
    def _serialize_msg_type(
        self, value: GroupChatTypeEnum, _info: "FieldSerializationInfo",
    ) -> int:
        return value.value

    @property
    def as_introduction(self) -> "GroupChatPleaseAdd | None":
        """The announcement payload if this is a well-formed INTRODUCTION
        message, else None. Centralizes the (msg_type, introduction-present)
        check otherwise duplicated across the row-rendering and headless
        read-matching code paths.

        >>> intro = GroupChatPleaseAdd(display_name="alice", read_cap=bytes(136))
        >>> announce = GroupChatMessage(
        ...     version=0, membership_hash=bytes(32), introduction=intro,
        ... )
        >>> announce.as_introduction.display_name
        'alice'
        >>> GroupChatMessage(
        ...     version=0, membership_hash=bytes(32), text="hi",
        ... ).as_introduction is None
        True
        """
        if self.msg_type == GroupChatTypeEnum.INTRODUCTION and self.introduction is not None:
            return self.introduction
        return None

    def to_cbor(self) -> bytes:
        """A group chat message consists of one CBOR messages potentially
        serialized over one or more BACAP boxes.

        - When it consists of multiple, we need to write a "subchain"
        - When it consists of one, we can probably write it directly.

        - The exception is that when we need to send more than one message at the same time,

        >>> msg = GroupChatMessage(version=0, membership_hash=bytes(32), text="hi")
        >>> cbor2.loads(msg.to_cbor())["msg_type"]
        0
        >>> "introduction" in cbor2.loads(msg.to_cbor())
        False
        """
        return cbor2.dumps(self.model_dump(exclude_none=True))

    @classmethod
    def from_cbor(cls, cbor_bytes:bytes) -> "GroupChatMessage":
        """Decode a wire payload, inferring ``msg_type`` when it is absent.

        >>> legacy = cbor2.dumps(
        ...     {"version": 0, "membership_hash": bytes(32), "who": "alice"},
        ... )
        >>> GroupChatMessage.from_cbor(legacy).msg_type
        <GroupChatTypeEnum.WHO: 3>
        >>> msg = GroupChatMessage(version=0, membership_hash=bytes(32), text="hi")
        >>> GroupChatMessage.from_cbor(msg.to_cbor()) == msg
        True
        """
        return cls(**cbor2.loads(cbor_bytes)) # TODO not at all what we want but here we go

def unserialize(chunks: "Iterable[tuple[bytes, bytes]]") -> "GroupChatMessage | None":
    """Reassemble a serialised ``SendOperation`` chain into a
    :class:`GroupChatMessage`.

    ``chunks`` is an iterable of ``(chunk_type, chunk_bytes)`` tuples
    ordered by BACAP index. ``chunk_type`` is the single-byte framing
    marker emitted by :meth:`SendOperation.serialize`:

    * ``b'C'``: continuation; carries an interior slice of the
      CBOR-encoded message,
    * ``b'F'``: final; carries the last slice, terminating the chain,
    * ``b'I'``: indirection; reserved for the network-layer coalescer
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

    >>> blob = GroupChatMessage(
    ...     version=0, membership_hash=bytes(32), text="hi there",
    ... ).to_cbor()
    >>> unserialize([(b"C", blob[:5]), (b"F", blob[5:])]).text
    'hi there'
    >>> unserialize([(b"C", blob)]) is None
    True
    >>> unserialize([]) is None
    True
    >>> unserialize([(b"F", blob), (b"C", b"")])
    Traceback (most recent call last):
        ...
    ValueError: 'F' chunk at index 0 but 1 chunk(s) follow
    >>> unserialize([(b"X", blob)])
    Traceback (most recent call last):
        ...
    ValueError: unknown chunk type: b'X'
    """
    parts = []
    chunk_list = list(chunks)
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


# ConversationUIState moved to katzenqt.qt_models; see banner near the
# top of this file for rationale.
