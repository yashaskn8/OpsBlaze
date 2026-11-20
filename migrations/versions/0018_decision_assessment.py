
"""A4 typed deterministic hard-gate assessments, not calibrated risk."""

from alembic import op
import sqlalchemy as sa

revision = "0018_decision_assessment"
down_revision = "0017_failure_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_assessment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("threshold_version", sa.String(32), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_id", "step_no", name="uq_assessment_step"),
    )
    op.create_index(
        "ix_assessment_tenant_incident", "decision_assessment",
        ["tenant_id", "incident_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assessment_tenant_incident", table_name="decision_assessment")
    op.drop_table("decision_assessment")
