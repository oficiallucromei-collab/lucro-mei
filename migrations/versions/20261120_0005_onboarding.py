"""Onboarding state, terms acceptance and free trial."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0005"
down_revision = "20261120_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("display_name", sa.String(60), nullable=True))
    op.add_column("businesses", sa.Column("business_type", sa.String(80), nullable=True))
    # Existing businesses never accepted terms, so they go through onboarding too.
    op.add_column(
        "businesses",
        sa.Column("onboarding_step", sa.String(30), nullable=False, server_default="new"),
    )
    op.add_column(
        "businesses", sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "businesses", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "terms_acceptances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("terms_version", sa.String(32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("accepted_text", sa.String(100), nullable=False),
        sa.UniqueConstraint("business_id", "terms_version", name="uq_terms_acceptances_version"),
    )


def downgrade() -> None:
    op.drop_table("terms_acceptances")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("trial_ends_at")
        batch.drop_column("trial_started_at")
        batch.drop_column("onboarding_step")
        batch.drop_column("business_type")
        batch.drop_column("display_name")
