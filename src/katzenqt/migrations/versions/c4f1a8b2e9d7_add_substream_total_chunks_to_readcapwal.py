"""add substream_total_chunks to readcapwal

Revision ID: c4f1a8b2e9d7
Revises: d08418a855a1

Carries the total plaintext chunk count (C-chunks + final F) of a substream
file transfer so the GUI can render download progress as n/total (TODO item 4).
NULL means the peer sent a legacy 136-byte I-chunk and the total is unknown.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f1a8b2e9d7'
down_revision: Union[str, Sequence[str], None] = 'd08418a855a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('substream_total_chunks', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('readcapwal', schema=None) as batch_op:
        batch_op.drop_column('substream_total_chunks')