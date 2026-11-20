"""P1 durable UI event notification cursor watermark after retention."""

from alembic import op
import sqlalchemy as sa

revision = "0028_ui_event_retention"
down_revision = "0027_worker_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_event_watermark",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"),
                  primary_key=True),
        sa.Column("last_purged_id", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ui_event_watermark")
