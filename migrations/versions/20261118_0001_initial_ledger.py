"""Create the immutable ledger tables."""

import sqlalchemy as sa
from alembic import op

revision = "20261118_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "businesses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("whatsapp_number", sa.String(32), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inbound_messages_business_id", "inbound_messages", ["business_id"])
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column(
            "source_message_id",
            sa.String(36),
            sa.ForeignKey("inbound_messages.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("transaction_type", sa.String(20), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_transactions_positive_amount"),
    )
    op.create_index("ix_transactions_business_id", "transactions", ["business_id"])
    op.create_index("ix_transactions_occurred_at", "transactions", ["occurred_at"])
    op.create_table(
        "transaction_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "transaction_id", sa.String(36), sa.ForeignKey("transactions.id"), nullable=False
        ),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_transaction_events_transaction_id", "transaction_events", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_table("transaction_events")
    op.drop_table("transactions")
    op.drop_table("inbound_messages")
    op.drop_table("businesses")
