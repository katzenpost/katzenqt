from alembic import op
import sqlalchemy as sa

revision: str = "31a0f3b426c8"
down_revision: str = "1fa24637fdae"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    with op.batch_alter_table("readcapwal") as table:
        table.add_column(sa.Column(
            "read_paused", sa.Boolean(), nullable=False,
            server_default=sa.text("0"),
        ))


def downgrade() -> None:
    with op.batch_alter_table("readcapwal") as table:
        table.drop_column("read_paused")
