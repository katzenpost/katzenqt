"""conversation voucher_used flag

Revision ID: 35cec50b9604
Revises: d08418a855a1
Create Date: 2026-09-11 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = '35cec50b9604'
down_revision: Union[str, Sequence[str], None] = 'd08418a855a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('conversation', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'voucher_used', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversation', schema=None) as batch_op:
        batch_op.drop_column('voucher_used')
