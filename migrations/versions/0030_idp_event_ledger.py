"""Replay-safe ledger for signed identity-provider security events."""

from alembic import op
import sqlalchemy as sa

revision = "0030_idp_event_ledger"
down_revision = "0029_identity_revocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_provider_event",
        sa.Column("event_digest", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(48), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("identity_provider_event")
