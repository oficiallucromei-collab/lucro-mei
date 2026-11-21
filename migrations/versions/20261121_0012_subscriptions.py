"""Subscriptions: the paid plan after the free trial (no provider is active yet)."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0012"
down_revision = "20261121_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_customer_id", sa.String(255), nullable=True),
        sa.Column("provider_subscription_id", sa.String(255), nullable=True, unique=True),
        sa.Column("provider_checkout_id", sa.String(255), nullable=True, unique=True),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("price_cents >= 0", name="ck_subscriptions_price"),
    )
    op.create_index("ix_subscriptions_business_status", "subscriptions", ["business_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_business_status", table_name="subscriptions")
    op.drop_table("subscriptions")
