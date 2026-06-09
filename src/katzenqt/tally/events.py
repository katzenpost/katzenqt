"""Build the group-chat messages that carry tally protocol events.

Each builder returns a :class:`katzenqt.models.GroupChatMessage` with the
explicit ``msg_type`` set and a :class:`katzenqt.models.GroupChatTally`
payload populated as that kind requires. These are pure: they touch no
network and no database.
"""
from __future__ import annotations

from ..models import GroupChatMessage, GroupChatTally, GroupChatTypeEnum

# A tally message is not a membership event, so it carries no membership hash;
# the field is required to be 32 bytes, so we supply a fixed zero block.
_NO_MEMBERSHIP = bytes(32)


def _message(kind: GroupChatTypeEnum, tally: GroupChatTally) -> GroupChatMessage:
    return GroupChatMessage(
        version=0, membership_hash=_NO_MEMBERSHIP, msg_type=kind, tally=tally,
    )


def build_create(survey_id: bytes, full_state: bytes) -> GroupChatMessage:
    """Broadcast a new survey: the whole initial Doc as one update."""
    return _message(
        GroupChatTypeEnum.TALLY_CREATE,
        GroupChatTally(survey_id=survey_id, crdt=full_state),
    )


def build_vote(survey_id: bytes, choice: "dict[str, str]", version: int = 0) -> GroupChatMessage:
    """A semantic vote. The receiver records it under the authenticated
    sender's key, never an id from the payload."""
    return _message(
        GroupChatTypeEnum.TALLY_VOTE,
        GroupChatTally(survey_id=survey_id, choice=choice, version=version),
    )


def build_close(survey_id: bytes, version: int = 0) -> GroupChatMessage:
    return _message(
        GroupChatTypeEnum.TALLY_CLOSE,
        GroupChatTally(survey_id=survey_id, version=version),
    )


def build_sync_request(survey_id: bytes, state_vector: bytes) -> GroupChatMessage:
    return _message(
        GroupChatTypeEnum.TALLY_SYNC_REQ,
        GroupChatTally(survey_id=survey_id, crdt=state_vector),
    )


def build_sync_response(survey_id: bytes, diff: bytes) -> GroupChatMessage:
    return _message(
        GroupChatTypeEnum.TALLY_SYNC_RESP,
        GroupChatTally(survey_id=survey_id, crdt=diff),
    )
