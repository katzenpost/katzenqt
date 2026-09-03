"""drop mixwal destination column

Revision ID: d08418a855a1
Revises: 0711d7a23cd9
Create Date: 2026-07-05 15:59:30.894376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd08418a855a1'
down_revision: Union[str, Sequence[str], None] = '0711d7a23cd9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('mixwal', schema=None) as batch_op:
        batch_op.drop_column('destination')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('mixwal', schema=None) as batch_op:
        batch_op.add_column(sa.Column('destination', sa.LargeBinary(), nullable=False))
