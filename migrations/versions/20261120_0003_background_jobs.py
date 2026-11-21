"""Record inbound WhatsApp events and process them through a PostgreSQL job queue."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0003"
down_revision = "20261118_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("sender", sa.String(32), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_inbound_events_sender_status", "inbound_events", ["sender", "status"])
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipient", sa.String(32), nullable=False),
        sa.Column("in_reply_to", sa.String(255), nullable=True, unique=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(255), nullable=True, unique=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("outbound_messages")
    op.drop_table("inbound_events")
