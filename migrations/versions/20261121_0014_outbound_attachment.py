"""Outbound messages can carry a document built at send time (the spreadsheet export)."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0014"
down_revision = "20261121_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbound_messages", sa.Column("attachment", sa.JSON(none_as_null=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("outbound_messages", "attachment")
