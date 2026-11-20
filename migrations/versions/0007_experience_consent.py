
"""E1 explicit consent and metadata-only experience capsules."""

from alembic import op
import sqlalchemy as sa

revision = "0007_experience_consent"
down_revision = "0006_outcome_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experience_consent",
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "experience_capsule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("outcome_revision_id", sa.Uuid(), sa.ForeignKey("outcome_revision.id"), nullable=False),
        sa.Column("consent_epoch", sa.Integer(), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("incident_id", "outcome_revision_id", name="uq_capsule_outcome"),
    )
    op.create_index(
        "ix_experience_scoped", "experience_capsule",
        ["tenant_id", "incident_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_experience_scoped", table_name="experience_capsule")
    op.drop_table("experience_capsule")
    op.drop_table("experience_consent")
