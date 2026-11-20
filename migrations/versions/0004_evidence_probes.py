"""A1/A2/A3 read-only probe and immutable evidence foundation."""

from alembic import op
import sqlalchemy as sa

revision = "0004_evidence_probes"
down_revision = "0003_admission_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incident", sa.Column("probe_used", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(32), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("redacted_excerpt", sa.String(512), nullable=False),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column("validity", sa.String(24), nullable=False),
    )
    op.create_index("ix_evidence_scoped", "evidence", ["tenant_id", "incident_id", "captured_at", "id"])
    op.create_table(
        "probe_call",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("catalog_id", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), sa.ForeignKey("evidence.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_id", "step_no", name="uq_probe_incident_step"),
    )
    op.create_table(
        "hypothesis_step",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("cause_scores", sa.JSON(), nullable=False),
        sa.Column("probe_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(48), nullable=False),
        sa.Column("token_reserved", sa.Integer(), nullable=False),
        sa.Column("token_used", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_id", "step_no", name="uq_hypothesis_incident_step"),
    )


def downgrade() -> None:
    op.drop_table("hypothesis_step")
    op.drop_table("probe_call")
    op.drop_index("ix_evidence_scoped", table_name="evidence")
    op.drop_table("evidence")
    op.drop_column("incident", "probe_used")
