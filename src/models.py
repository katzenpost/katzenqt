from pydantic import Field, BaseModel
from qt_models import *
from PySide6.QtQml import QQmlPropertyMap

class ConversationUIState(BaseModel):
    class Config:
        validate_assignment = True
        arbitrary_types_allowed = True
    conversation_id : int
    own_peer_id : int = Field(description="ConversationPeer.id for self")
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
