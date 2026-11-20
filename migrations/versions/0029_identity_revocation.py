"""P2 persistent tenant-scoped security-authority revocation gate."""

from alembic import op
import sqlalchemy as sa

revision = "0029_identity_revocation"
down_revision = "0028_ui_event_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revoked_identity",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(48), primary_key=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.String(48), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("revoked_identity")
