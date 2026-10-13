"""group chat state

Revision ID: 3bee8c5a8252
Revises: 3822561b8c5b
Create Date: 2026-10-13 12:00:00.000000

A conversation's group state, kept as one ``katzenqt.groupchat`` state blob:
where each of our messages landed, with what the box holds, until the blob
takes it in, and the rewrites the state machine asks for until they are sent.
Each message in a conversation's log notes when it was added, for the
disappearing messages policy, and messages already there count from now. Each
message of ours notes the ``Idx64`` of the box it ends in, big-endian so that
SQLite orders it as a number, to match against members' acknowledgements.
"""
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3bee8c5a8252'
down_revision: Union[str, Sequence[str], None] = '3822561b8c5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('groupstate',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('state', sa.LargeBinary(), nullable=False),
    sa.Column('held', sa.LargeBinary(), nullable=False),
    sa.Column('outgoing', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_groupstate_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', name=op.f('pk_groupstate'))
    )
    op.create_table('groupwritten',
    sa.Column('pwal_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.Column('payload', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_groupwritten_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('pwal_id', name=op.f('pk_groupwritten'))
    )
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_groupwritten_conversation_id'), ['conversation_id'], unique=False)
    op.create_table('grouprewrite',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.Column('payload', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_grouprewrite_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'box_index', name=op.f('pk_grouprewrite'))
    )
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.add_column(sa.Column('logged_at', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('sent_idx64', sa.LargeBinary(), nullable=True))
    op.execute(sa.text("UPDATE conversationlog SET logged_at = :now").bindparams(now=time.time()))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversationlog', schema=None) as batch_op:
        batch_op.drop_column('sent_idx64')
        batch_op.drop_column('logged_at')
    op.drop_table('grouprewrite')
    with op.batch_alter_table('groupwritten', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_groupwritten_conversation_id'))
    op.drop_table('groupwritten')
    op.drop_table('groupstate')
