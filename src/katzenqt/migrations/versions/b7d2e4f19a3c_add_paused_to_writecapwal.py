"""add paused to writecapwal

Revision ID: b7d2e4f19a3c
Revises: c4f1a8b2e9d7

Marks an outbound substream's WriteCapWAL as paused so the chunk sweep skips
it until the user resumes. The main conversation stream is never paused, and
a paused upload stays paused across a restart.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f19a3c'
down_revision: Union[str, Sequence[str], None] = 'c4f1a8b2e9d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('writecapwal', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'paused', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('writecapwal', schema=None) as batch_op:
        batch_op.drop_column('paused')
