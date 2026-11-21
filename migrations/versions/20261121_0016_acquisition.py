"""Acquisition: campaign clicks and first-touch attribution (renumbered from the old branch)."""

import sqlalchemy as sa
from alembic import op

from lucromei.dbgrants import grant_runtime

revision = "20261121_0016"
down_revision = "20261121_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_clicks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("business_id", sa.String(36), nullable=True),
    )
    op.create_table(
        "acquisitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "business_id", sa.String(36), sa.ForeignKey("businesses.id"), nullable=False,
            unique=True,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("medium", sa.String(64), nullable=True),
        sa.Column("campaign", sa.String(64), nullable=True),
        sa.Column("content", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    grant_runtime("acquisition_clicks", ("SELECT", "INSERT", "UPDATE", "DELETE"))
    grant_runtime("acquisitions", ("SELECT", "INSERT", "DELETE"))


def downgrade() -> None:
    op.drop_table("acquisitions")
    op.drop_table("acquisition_clicks")
