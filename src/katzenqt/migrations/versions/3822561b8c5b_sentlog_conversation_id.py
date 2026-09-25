"""sentlog conversation_id

Revision ID: 3822561b8c5b
Revises: 31a0f3b426c8

Records which conversation a sent message belonged to, so removing a
conversation can delete its SentLog rows. NULL for rows written earlier, which
cannot be attributed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3822561b8c5b"
down_revision: Union[str, Sequence[str], None] = "31a0f3b426c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sentlog", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("conversation_id", sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_sentlog_conversation_id"),
            ["conversation_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_sentlog_conversation_id_conversation"),
            "conversation",
            ["conversation_id"],
            ["id"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sentlog", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_sentlog_conversation_id_conversation"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_sentlog_conversation_id"))
        batch_op.drop_column("conversation_id")
