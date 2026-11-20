
"""Plan decisions and durable signals for the simulator-only P2 gateway."""

from alembic import op
import sqlalchemy as sa

revision = "0005_plan_decision"
down_revision = "0004_evidence_probes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("payload", sa.JSON(), nullable=True))
    op.add_column(
        "resource_reservation",
        sa.Column("expected_version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "incident_plan",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("incident_id", name="uq_plan_incident"),
    )
    op.create_index("ix_plan_tenant_incident", "incident_plan", ["tenant_id", "incident_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_tenant_incident", table_name="incident_plan")
    op.drop_table("incident_plan")
    op.drop_column("resource_reservation", "expected_version")
    op.drop_column("outbox", "payload")
