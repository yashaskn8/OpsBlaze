"""P3 scoped evidence-edge projection; source records remain authoritative."""

from alembic import op
import sqlalchemy as sa

revision = "0042_lineage_edge"
down_revision = "0041_trace_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineage_edge",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("from_type", sa.String(16), primary_key=True),
        sa.Column("from_id", sa.Uuid(), primary_key=True),
        sa.Column("to_type", sa.String(16), primary_key=True),
        sa.Column("to_id", sa.Uuid(), primary_key=True),
        sa.Column("relation", sa.String(24), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "to_type = 'evidence' AND ((from_type = 'hypothesis' AND relation = 'CITES') "
            "OR (from_type = 'plan' AND relation = 'JUSTIFIED_BY'))", name="ck_lineage_edge_evidence_relation",
        ),
    )
    op.create_index("ix_lineage_edge_incident_created", "lineage_edge", ["tenant_id", "incident_id", "created_at"])
    op.create_index("ix_lineage_edge_source", "lineage_edge", ["from_type", "from_id"])


def downgrade() -> None:
    op.drop_index("ix_lineage_edge_source", table_name="lineage_edge")
    op.drop_index("ix_lineage_edge_incident_created", table_name="lineage_edge")
    op.drop_table("lineage_edge")
