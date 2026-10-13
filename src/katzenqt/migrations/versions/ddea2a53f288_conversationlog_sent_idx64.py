"""conversationlog sent_idx64

Revision ID: ddea2a53f288
Revises: daf7db355b72
Create Date: 2026-10-13 09:00:00.000000

Each message of ours notes the Idx64 of the box it ends in, big-endian so
that SQLite orders it as a number, instead of where it falls in the order we
wrote our stream. A message whose box the Sent box still records is carried
over; any other loses its place, and is never marked acknowledged.
"""
from typing import Sequence, Union

from alembic import op
import cbor2
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ddea2a53f288'
down_revision: Union[str, Sequence[str], None] = 'daf7db355b72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _idx64_by_seq(state: bytes) -> dict[int, bytes]:
    """The Idx64 of each box the stored Sent box records, by its place."""
    try:
        _, body = cbor2.loads(state)
        records = body["sent"]["records"]
        return {
            record["seq"]: int.from_bytes(index[:8], "little").to_bytes(8, "big")
            for index, record in records
        }
    except (cbor2.CBORDecodeError, KeyError, TypeError, ValueError):
        return {}


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sent_idx64', sa.LargeBinary(), nullable=True))
    bind = op.get_bind()
    for conversation_id, state in bind.execute(
        sa.text("SELECT conversation_id, state FROM groupstate")
    ).all():
        for seq, idx64 in _idx64_by_seq(state).items():
            bind.execute(
                sa.text(
                    "UPDATE conversationlog SET sent_idx64 = :idx64 "
                    "WHERE conversation_id = :conversation_id AND sent_seq = :seq"
                ),
                {"idx64": idx64, "conversation_id": conversation_id, "seq": seq},
            )
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.drop_column('sent_seq')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sent_seq', sa.Integer(), nullable=True))
        batch_op.drop_column('sent_idx64')
