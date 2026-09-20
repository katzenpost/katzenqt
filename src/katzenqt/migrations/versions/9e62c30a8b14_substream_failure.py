from alembic import op
import sqlalchemy as sa

revision: str = "9e62c30a8b14"
down_revision: str = "c4f1a8b2e9d7"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    with op.batch_alter_table("readcapwal") as table:
        table.add_column(sa.Column("substream_missing_since", sa.Float()))
        table.add_column(sa.Column("substream_failure", sa.String()))


def downgrade() -> None:
    with op.batch_alter_table("readcapwal") as table:
        table.drop_column("substream_failure")
        table.drop_column("substream_missing_since")
