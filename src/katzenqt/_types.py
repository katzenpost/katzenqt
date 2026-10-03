import asyncio
from typing import Annotated, TypeAlias

ConversationId: TypeAlias = Annotated[int, "conversation.id"]

ConversationUpdate: TypeAlias = tuple[ConversationId, bool]
ConversationUpdateQueue: TypeAlias = "asyncio.Queue[ConversationUpdate]"
TallyEventQueue: TypeAlias = "asyncio.Queue[ConversationId]"

PkiDocument: TypeAlias = dict[str, object]

__all__ = [
    "ConversationId",
    "ConversationUpdate",
    "ConversationUpdateQueue",
    "PkiDocument",
    "TallyEventQueue",
]
