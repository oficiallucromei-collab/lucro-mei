"""Keep the request's correlation ID on background jobs."""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0008"
down_revision = "20261120_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("correlation_id", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("correlation_id")
