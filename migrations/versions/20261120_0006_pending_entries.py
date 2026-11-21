"""Pending entries: transactions waiting for the user to confirm their type."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0006"
down_revision = "20261120_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("source_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_types", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_message_id", sa.String(255), nullable=True),
        sa.Column(
            "transaction_id", sa.String(36), sa.ForeignKey("transactions.id"), nullable=True
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_pending_entries_positive_amount"),
    )
    op.create_index(
        "uq_pending_entries_one_open_per_business",
        "pending_entries",
        ["business_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_table("pending_entries")
