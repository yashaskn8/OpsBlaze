
"""E3 deterministic taxonomy and constrained improvement proposals."""

from alembic import op
import sqlalchemy as sa

revision = "0008_failure_proposals"
down_revision = "0007_experience_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "failure_cluster",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "signature", "version", name="uq_failure_cluster_signature"
        ),
    )
    op.create_table(
        "failure_case",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), nullable=False),
        sa.Column("outcome_revision_id", sa.Uuid(), sa.ForeignKey("outcome_revision.id"), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), sa.ForeignKey("failure_cluster.id"), nullable=False),
        sa.Column("origin_code", sa.String(32), nullable=False),
        sa.Column("first_bad_step", sa.Integer(), nullable=True),
        sa.Column("classification_version", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "outcome_revision_id", name="uq_failure_outcome"),
    )
    op.create_index(
        "ix_failure_tenant_origin", "failure_case",
        ["tenant_id", "origin_code", "created_at"],
    )
    op.create_table(
        "improvement_candidate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_cluster_id", sa.Uuid(), sa.ForeignKey("failure_cluster.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_policy_id", sa.String(64), nullable=False),
        sa.Column("change_spec", sa.JSON(), nullable=False),
        sa.Column("hypothesis", sa.String(512), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("evaluation_manifest_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_improvement_state", "improvement_candidate",
        ["tenant_id", "state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_improvement_state", table_name="improvement_candidate")
    op.drop_table("improvement_candidate")
    op.drop_index("ix_failure_tenant_origin", table_name="failure_case")
    op.drop_table("failure_case")
    op.drop_table("failure_cluster")
