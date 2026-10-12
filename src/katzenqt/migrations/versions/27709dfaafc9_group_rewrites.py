"""group rewrites

Revision ID: 27709dfaafc9
Revises: 5b1e8c27d4a9
Create Date: 2026-10-12 12:48:09.191309

Where each of our messages landed is noted with what the box holds, so the
Sent box can rewrite it, and the rewrites the group state machine asks for
are queued until sent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27709dfaafc9'
down_revision: Union[str, Sequence[str], None] = '5b1e8c27d4a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('grouprewrite',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.Column('payload', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_grouprewrite_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'box_index', name=op.f('pk_grouprewrite'))
    )
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payload', sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.drop_column('payload')

    op.drop_table('grouprewrite')
