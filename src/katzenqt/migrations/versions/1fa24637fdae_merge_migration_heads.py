"""Merge migration heads

Revision ID: 1fa24637fdae
Revises: 31a0f3b426c8, b7d2e4f19a3c
Create Date: 2026-09-22 01:09:18.264517

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '1fa24637fdae'
down_revision: Union[str, Sequence[str], None] = ('9e62c30a8b14', 'b7d2e4f19a3c')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
