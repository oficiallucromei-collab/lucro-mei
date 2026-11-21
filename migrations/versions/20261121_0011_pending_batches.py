"""Pending batches: several entries from one message, previewed and confirmed together."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0011"
down_revision = "20261121_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_entries",
        sa.Column("kind", sa.String(20), nullable=False, server_default="single"),
    )
    op.add_column("pending_entries", sa.Column("items", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pending_entries", "items")
    op.drop_column("pending_entries", "kind")
