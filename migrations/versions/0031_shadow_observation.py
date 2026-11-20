"""E6 persistent shadow-preview decisions, never live policy assignment."""

from alembic import op
import sqlalchemy as sa

revision = "0031_shadow_observation"
down_revision = "0030_idp_event_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_shadow_observation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "rollout_id", sa.Uuid(), sa.ForeignKey("policy_rollout.id"),
            nullable=False,
        ),
        sa.Column(
            "incident_id", sa.Uuid(), sa.ForeignKey("incident.id"),
            nullable=False,
        ),
        sa.Column("bundle_digest", sa.String(64), nullable=False),
        sa.Column("would_assign", sa.Boolean(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "rollout_id", "incident_id",
            name="uq_shadow_tenant_rollout_incident",
        ),
    )


def downgrade() -> None:
    op.drop_table("policy_shadow_observation")
