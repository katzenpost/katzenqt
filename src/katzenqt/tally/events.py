"""Build the group-chat messages that carry tally protocol events.

Each builder returns a :class:`katzenqt.models.GroupChatMessage` with the
explicit ``msg_type`` set and a :class:`katzenqt.models.GroupChatTally`
payload populated as that kind requires. These are pure: they touch no
network and no database.
"""
from __future__ import annotations

from ..models import GROUP_CHAT_VERSION, GroupChatMessage, GroupChatTally, GroupChatTypeEnum


def _message(kind: GroupChatTypeEnum, tally: GroupChatTally) -> GroupChatMessage:
    """Wrap a tally payload in a group-chat message of the given kind.

    >>> msg = _message(
    ...     GroupChatTypeEnum.TALLY_CLOSE, GroupChatTally(survey_id=bytes(16)))
    >>> msg.msg_type
    <GroupChatTypeEnum.TALLY_CLOSE: 7>
    >>> msg.text is None
    True
    """
    return GroupChatMessage(
        version=GROUP_CHAT_VERSION, msg_type=kind, tally=tally,
    )


def build_create(survey_id: bytes, full_state: bytes) -> GroupChatMessage:
    """Broadcast a new survey: the whole initial Doc as one update.

    >>> msg = build_create(bytes(16), b"crdt-update")
    >>> msg.msg_type
    <GroupChatTypeEnum.TALLY_CREATE: 5>
    >>> msg.tally.crdt, msg.tally.choice
    (b'crdt-update', None)
    """
    return _message(
        GroupChatTypeEnum.TALLY_CREATE,
        GroupChatTally(survey_id=survey_id, crdt=full_state),
    )


def build_vote(survey_id: bytes, choice: "dict[str, str]", version: int = 0) -> GroupChatMessage:
    """A semantic vote. The receiver records it under the authenticated
    sender's key, never an id from the payload.

    >>> msg = build_vote(bytes(16), {"s0": "yes"}, version=3)
    >>> msg.msg_type, msg.tally.version, msg.tally.choice
    (<GroupChatTypeEnum.TALLY_VOTE: 6>, 3, {'s0': 'yes'})
    >>> build_vote(bytes(16), {"s0": "yes"}).tally.version
    0
    """
    return _message(
        GroupChatTypeEnum.TALLY_VOTE,
        GroupChatTally(survey_id=survey_id, choice=choice, version=version),
    )


def build_close(survey_id: bytes, version: int = 0) -> GroupChatMessage:
    """Announce that the survey is closed; carries no CRDT payload.

    >>> msg = build_close(bytes(16), version=2)
    >>> msg.msg_type, msg.tally.version
    (<GroupChatTypeEnum.TALLY_CLOSE: 7>, 2)
    >>> msg.tally.crdt is None and msg.tally.choice is None
    True
    """
    return _message(
        GroupChatTypeEnum.TALLY_CLOSE,
        GroupChatTally(survey_id=survey_id, version=version),
    )


def build_sync_request(survey_id: bytes, state_vector: bytes) -> GroupChatMessage:
    """Ask peers for everything this Doc lacks, by sending its state vector.

    >>> msg = build_sync_request(bytes(16), b"state-vector")
    >>> msg.msg_type
    <GroupChatTypeEnum.TALLY_SYNC_REQ: 8>
    >>> msg.tally.crdt
    b'state-vector'
    """
    return _message(
        GroupChatTypeEnum.TALLY_SYNC_REQ,
        GroupChatTally(survey_id=survey_id, crdt=state_vector),
    )


def build_sync_response(survey_id: bytes, diff: bytes) -> GroupChatMessage:
    """Answer a sync request with the diff since the requester's state vector.

    >>> msg = build_sync_response(bytes(16), b"diff-bytes")
    >>> msg.msg_type
    <GroupChatTypeEnum.TALLY_SYNC_RESP: 9>
    >>> msg.tally.crdt
    b'diff-bytes'
    """
    return _message(
        GroupChatTypeEnum.TALLY_SYNC_RESP,
        GroupChatTally(survey_id=survey_id, crdt=diff),
    )
