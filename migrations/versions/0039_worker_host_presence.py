"""Track opt-in worker host presence without mistaking IDs for HA proof."""

from alembic import op
import sqlalchemy as sa

revision = "0039_worker_host_presence"
down_revision = "0038_receipt_permit_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_host_heartbeat",
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("host_id", sa.String(64), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(host_id) BETWEEN 1 AND 64",
            name="ck_worker_host_heartbeat_host_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("worker_host_heartbeat")
