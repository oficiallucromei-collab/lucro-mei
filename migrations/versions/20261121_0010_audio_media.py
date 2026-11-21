"""Keep a Meta media ID only while an inbound voice note is pending."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0010"
down_revision = "20261121_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inbound_events", sa.Column("media_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("inbound_events", "media_id")
