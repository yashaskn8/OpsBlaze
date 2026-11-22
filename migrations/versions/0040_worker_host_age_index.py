"""Index fresh host lookup for bounded G3 readiness on long-lived hosts."""

from alembic import op

revision = "0040_worker_host_age_index"
down_revision = "0039_worker_host_presence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_worker_host_role_seen", "worker_host_heartbeat",
        ["role", "last_seen"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_host_role_seen", table_name="worker_host_heartbeat",
    )
