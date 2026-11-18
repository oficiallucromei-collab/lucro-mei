"""Add idempotent conversational command receipts."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0002"
down_revision = "20261118_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("command_type", sa.String(20), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_command_receipts_business_id", "command_receipts", ["business_id"])


def downgrade() -> None:
    op.drop_table("command_receipts")
