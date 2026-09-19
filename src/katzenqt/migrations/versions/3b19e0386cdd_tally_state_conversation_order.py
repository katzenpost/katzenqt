"""tally_state conversation_order

Revision ID: 3b19e0386cdd
Revises: c4f1a8b2e9d7

The TallyState row gains an optional conversation_order so the GUI can place a
survey's virtual placeholder row at the point in the chat timeline where the
survey was first seen. NULL for surveys that predate the column.

This migration was merged onto main's chain (down_revision was 35cec50b9604
on both branches, producing two heads; it now sits on top of
c4f1a8b2e9d7 so the graph is linear again).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b19e0386cdd'
down_revision: Union[str, Sequence[str], None] = 'c4f1a8b2e9d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tallystate', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conversation_order', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_tallystate_conversation_order'),
            ['conversation_order'], unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tallystate', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tallystate_conversation_order'))
        batch_op.drop_column('conversation_order')