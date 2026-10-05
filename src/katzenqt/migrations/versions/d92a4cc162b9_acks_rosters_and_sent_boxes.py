"""acks rosters and sent boxes

Revision ID: d92a4cc162b9
Revises: 3822561b8c5b
Create Date: 2026-10-03 16:44:57.926551

What opportunistic acknowledgements need to be sent and read: where each
stream has been read to and acknowledged to, a record of every box we wrote,
and the facts every member's roster is rebuilt from.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd92a4cc162b9'
down_revision: Union[str, Sequence[str], None] = '3822561b8c5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('acklevel',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('acker_key', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=False),
    sa.Column('roster_index', sa.Integer(), nullable=False),
    sa.Column('reached', sa.LargeBinary(), nullable=False),
    sa.Column('value', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_acklevel_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'acker_key', 'position', 'roster_index', name=op.f('pk_acklevel'))
    )
    op.create_table('introductionseen',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('introducer_key', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=True),
    sa.Column('member_key', sa.LargeBinary(), nullable=False),
    sa.Column('pending_pwal', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_introductionseen_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_introductionseen'))
    )
    with op.batch_alter_table('introductionseen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_introductionseen_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_introductionseen_pending_pwal'), ['pending_pwal'], unique=False)

    op.create_table('rostermember',
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('member_key', sa.LargeBinary(), nullable=False),
    sa.Column('seen', sa.LargeBinary(), nullable=True),
    sa.Column('base_roster', sa.LargeBinary(), nullable=True),
    sa.Column('base_introducer', sa.LargeBinary(), nullable=True),
    sa.Column('base_position', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_rostermember_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('conversation_id', 'member_key', name=op.f('pk_rostermember'))
    )
    op.create_table('sentbox',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('bacap_stream', sa.Uuid(), nullable=False),
    sa.Column('box_index', sa.LargeBinary(), nullable=False),
    sa.Column('position', sa.LargeBinary(), nullable=False),
    sa.Column('written_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], name=op.f('fk_sentbox_conversation_id_conversation')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sentbox')),
    sa.UniqueConstraint('bacap_stream', 'box_index', name='uniq_sentbox_stream_and_index')
    )
    with op.batch_alter_table('sentbox', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sentbox_bacap_stream'), ['bacap_stream'], unique=False)
        batch_op.create_index(batch_op.f('ix_sentbox_conversation_id'), ['conversation_id'], unique=False)

    with op.batch_alter_table('conversationpeer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('acked_position', sa.LargeBinary(), nullable=True))

    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('frontier_index', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('last_read_index', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('acked_index', sa.LargeBinary(), nullable=True))



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.drop_column('acked_index')
        batch_op.drop_column('last_read_index')
        batch_op.drop_column('frontier_index')

    with op.batch_alter_table('conversationpeer', schema=None) as batch_op:
        batch_op.drop_column('acked_position')

    with op.batch_alter_table('sentbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sentbox_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_sentbox_bacap_stream'))

    op.drop_table('sentbox')
    op.drop_table('rostermember')
    with op.batch_alter_table('introductionseen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_introductionseen_pending_pwal'))
        batch_op.drop_index(batch_op.f('ix_introductionseen_conversation_id'))

    op.drop_table('introductionseen')
    op.drop_table('acklevel')
