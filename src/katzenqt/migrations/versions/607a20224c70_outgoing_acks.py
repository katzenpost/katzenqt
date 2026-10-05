"""outgoing acks

Revision ID: 607a20224c70
Revises: d92a4cc162b9
Create Date: 2026-10-03 16:51:06.652609

The acknowledgements a queued message carries, kept until the message is
written and its position on our stream is known.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '607a20224c70'
down_revision: Union[str, Sequence[str], None] = 'd92a4cc162b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('outgoingacks',
    sa.Column('pwal_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('acker_key', sa.LargeBinary(), nullable=False),
    sa.Column('levels', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_outgoingacks_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('pwal_id', name=op.f('pk_outgoingacks'))
    )
    with op.batch_alter_table('outgoingacks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_outgoingacks_conversation_id'), ['conversation_id'], unique=False)



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('outgoingacks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outgoingacks_conversation_id'))

    op.drop_table('outgoingacks')
