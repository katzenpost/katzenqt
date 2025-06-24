from pydantic import Field, BaseModel
from qt_models import *

class ConversationUIState(BaseModel):
    class Config:
        validate_assignment = True
        arbitrary_types_allowed = True
    conversation_id : int
    own_peer_id : int = Field(description="ConversationPeer.id for self")
    chat_lineEdit_buffer : str
    conversation_log_model: ConversationLogModel
    chat_lines_scroll_idx : int | None
    # TODO: should store scroll state of self.ui.ChatLines
    # ie self.ui.ChatLines.scrollToBottom() for default new ones
    last_push_to_talk_ns : int = 0
    attached_files : set[str] = Field(default_factory=set)
