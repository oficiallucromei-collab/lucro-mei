"""Payment provider webhooks, stored once each (Stripe, test mode)."""

import sqlalchemy as sa
from alembic import op

from lucromei.dbgrants import grant_runtime

revision = "20261121_0015"
down_revision = "20261121_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("provider_subscription_id", sa.String(255), nullable=True),
        sa.Column("subscription_id", sa.String(36), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    grant_runtime("subscription_events", ("SELECT", "INSERT", "DELETE"))


def downgrade() -> None:
    op.drop_table("subscription_events")
