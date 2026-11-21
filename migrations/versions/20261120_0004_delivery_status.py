"""Record WhatsApp delivery receipts for messages we sent."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0004"
down_revision = "20261120_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_status_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_message_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider_message_id", "status", name="uq_whatsapp_status_events_message_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_status_events")
