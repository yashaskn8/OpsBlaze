"""Readiness requires recent dispatcher and Temporal worker presence."""

from alembic import op
import sqlalchemy as sa

revision = "0027_worker_heartbeat"
down_revision = "0026_approval_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeat",
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
