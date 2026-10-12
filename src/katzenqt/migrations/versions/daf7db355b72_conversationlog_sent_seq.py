"""conversationlog sent_seq

Revision ID: daf7db355b72
Revises: 2ed1df88e21d
Create Date: 2026-10-12 16:14:56.895459

Each message of ours notes where it falls in the order we wrote our
stream, so a member's acknowledgement can mark it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'daf7db355b72'
down_revision: Union[str, Sequence[str], None] = '2ed1df88e21d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sent_seq', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.drop_column('sent_seq')
