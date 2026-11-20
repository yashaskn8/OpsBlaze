"""Add durable simulator targets, fence epochs, and effect receipts."""

from alembic import op
import sqlalchemy as sa

revision = "0002_simulator_effects"
down_revision = "0001_incident_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resource_epoch",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("target_id", sa.String(256), primary_key=True),
        sa.Column("latest_fence", sa.BigInteger(), nullable=False),
        sa.Column("version_seen", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "resource_reservation",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("target_id", sa.String(256), primary_key=True),
        sa.Column("holder_incident", sa.Uuid(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
    )
    op.create_table(
        "simulator_target",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("target_id", sa.String(256), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("last_fence", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "simulator_applied_effect",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("observed_version", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "effect_receipt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("adapter_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("observed_version", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_effect_tenant_key"),
    )
    op.create_index("ix_effect_incident", "effect_receipt", ["tenant_id", "incident_id", "created_at"])
    op.create_index("ix_effect_unresolved", "effect_receipt", ["state", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_effect_unresolved", table_name="effect_receipt")
    op.drop_index("ix_effect_incident", table_name="effect_receipt")
    op.drop_table("effect_receipt")
    op.drop_table("simulator_applied_effect")
    op.drop_table("simulator_target")
    op.drop_table("resource_reservation")
    op.drop_table("resource_epoch")
