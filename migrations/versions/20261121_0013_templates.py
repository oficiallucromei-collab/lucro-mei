"""Proactive notices: Meta template fields on outbound messages and the reminder opt-out."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0013"
down_revision = "20261121_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column("template_name", sa.String(64), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("template_language", sa.String(10), nullable=True)
    )
    op.add_column(
        "outbound_messages",
        sa.Column("template_params", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("reminders_opt_out_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("businesses", "reminders_opt_out_at")
    op.drop_column("outbound_messages", "template_params")
    op.drop_column("outbound_messages", "template_language")
    op.drop_column("outbound_messages", "template_name")
