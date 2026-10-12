"""message times

Revision ID: 2ed1df88e21d
Revises: 27709dfaafc9
Create Date: 2026-10-12 14:02:11.000000

Each message in a conversation's log notes when it was added, for the
disappearing messages policy. Messages already there count from now.
"""
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ed1df88e21d'
down_revision: Union[str, Sequence[str], None] = '27709dfaafc9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('logged_at', sa.Float(), nullable=True))
    op.execute(sa.text("UPDATE conversationlog SET logged_at = :now").bindparams(now=time.time()))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.drop_column('logged_at')
