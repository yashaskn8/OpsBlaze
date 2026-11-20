
"""E7 pinned per-incident deterministic strategy routing."""

from alembic import op
import sqlalchemy as sa

revision = "0011_pinned_routes"
down_revision = "0010_evaluation_smoke"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "route_decision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("decision_no", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.String(64), nullable=False),
        sa.Column("context_digest", sa.String(64), nullable=False),
        sa.Column("risk_band", sa.String(16), nullable=False),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column("selection_reason", sa.String(64), nullable=False),
        sa.Column("propensity", sa.Integer(), nullable=False),
        sa.Column("cost_cap_probes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "incident_id", "decision_no", name="uq_route_once"),
    )
    op.create_index(
        "ix_route_tenant_created", "route_decision",
        ["tenant_id", "created_at", "route"],
    )


def downgrade() -> None:
    op.drop_index("ix_route_tenant_created", table_name="route_decision")
    op.drop_table("route_decision")
