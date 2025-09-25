import annotated_types
from typing_extensions import Annotated
from pydantic import Field, BaseModel, SecretBytes, SecretStr, Strict
from qt_models import *
from PySide6.QtQml import QQmlPropertyMap
import cbor2
from enum import Enum
import uuid
import secrets
import io
import persistent
from base64 import b64encode, b64decode
from typing import List

class GroupChatTEXT(BaseModel):
    model_config = {
        'validate_assignment': True }
    payload: str

class GroupChatPleaseAdd(BaseModel):
    """https://katzenpost.network/docs/specs/group_chat.html"""
    model_config = {
        'validate_assignment': True
    }
    display_name: str = Field(min_length=1, max_length=30)
    read_cap: bytes # TODO length should be 168 or whatever
    def to_cbor(self) -> bytes:
        return cbor2.dumps(self.model_dump(exclude_none=True))
    def to_human_readable(self) -> str:
        return b64encode(self.to_cbor()).replace(b'\n',b'').strip().decode()
    @classmethod
    def from_human_readable(cls, text:str) -> "GroupChatPleaseAdd":
        return cls(**cbor2.loads(b64decode(text.strip().encode()))) # TODO this can obv fail

class GroupChatTypeEnum(Enum):
    TEXT = 0
    INTRODUCTION = 1
    FILE_UPLOAD = 2
    WHO = 3
    REPLY_WHO = 4

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
                    id=None,
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
        agg_stream_read_cap = b"abcdef" # TODO
        agg.append(
            persistent.PlaintextWAL(
                id=None,
                after_id=None,
                after_stream=agg_bacap_stream,
                bacap_stream=self.bacap_stream,
                conversation_id=conversation_id,
                bacap_payload=b'I' + agg_stream_read_cap,
            )
        )
        return [agg_bacap_stream], agg

    def unserialize(self):
        # take a typing.IO param
        # first thing we see is either a b'I' or b'F'
        # while b'I' or b'C':
        #   yield things to read
        #   those things should get read and put in our box cache
        #   once read, continue the unserialize operation
        # return once we see a F
        # when we see a I we can start a new nested unserialize operation
        # that proceeds from the next message
        # data = io.BytesIO(b"123")
        # dec = cbor2.CBORDecoder(fp=data)
        # dec.decode()
        # dec.fp.tell() # pos
        # dec.fp.read() # remaining
        pass

class GroupChatFileUpload(BaseModel):
    model_config = {'validate_assignment': True}
    payload : bytes
    filetype: str # "image, sound, arbitrary"
    basename: str

class GroupChatMessage(BaseModel):
    """
    """
    model_config = {'validate_assignment': True}
    version: int = Field(ge=0,)
    membership_hash : "Annotated[bytes, Strict(), annotated_types.Len(32, 32),]"

    text: str | None = Field(default=None)
    introduction: GroupChatPleaseAdd | None = Field(default=None)
    file_upload: GroupChatFileUpload | None = Field(default=None)
    who: str | None = Field(default=None)
    reply_who: str | None = Field(default=None)
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

class ConversationUIState(BaseModel):
    model_config = {
        'validate_assignment': True,
        'arbitrary_types_allowed': True
    }
    conversation_id : int
    own_peer_id : int = Field(description="ConversationPeer.id for self")
    own_peer_bacap_uuid: uuid.UUID
    chat_lineEdit_buffer : str
    conversation_log_model: ConversationLogModel
    chat_lines_scroll_idx : float = 0.0
    # TODO: should store scroll state of self.ui.ChatLines
    # ie self.ui.ChatLines.scrollToBottom() for default new ones
    last_push_to_talk_ns : int = 0
    attached_files : set[str] = Field(default_factory=set)

    first_unread : int = 0
    # (projected) ConversationLog.conversation_order of first message the user
    # hasn't "read" yet - it doesn't have to exist in ConversationLog yet.

    def qml_ctx(self, rootObject:QObject|None) -> QQmlPropertyMap:
        props = QQmlPropertyMap(rootObject)
        props.insert({
            "chatTreeViewModel": self.conversation_log_model,
            "conversation_scroll": self.chat_lines_scroll_idx,
            "first_unread": self.first_unread,
        })
        return props

    async def update_first_unread(self, new_first_unread:int) -> None:
        """Update first_unread in the persistent database:"""
        if new_first_unread == self.first_unread:
            return
        print("UPDATED FIRST_UNREAD", self.first_unread, new_first_unread)
        self.first_unread = new_first_unread
        async with persistent.asession() as sess:
            co = await sess.get(persistent.Conversation, self.conversation_id)
            co.first_unread = self.first_unread
            sess.add(co)
            await sess.commit()
