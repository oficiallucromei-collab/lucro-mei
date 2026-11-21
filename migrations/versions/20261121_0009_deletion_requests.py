"""LGPD deletion requests."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0009"
down_revision = "20261120_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(36), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirm_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_deletion_requests_status_scheduled", "deletion_requests", ["status", "scheduled_for"]
    )
    op.create_index("ix_deletion_requests_business", "deletion_requests", ["business_id"])


def downgrade() -> None:
    op.drop_table("deletion_requests")
