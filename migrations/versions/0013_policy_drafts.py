
"""E6 immutable policy and unexposed rollout drafts."""

from alembic import op
import sqlalchemy as sa

revision = "0013_policy_drafts"
down_revision = "0012_adapter_conformance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_bundle",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("improvement_candidate.id"), nullable=False),
        sa.Column("evaluation_manifest_id", sa.Uuid(), sa.ForeignKey("evaluation_manifest.id"), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "content_digest", name="uq_policy_content"),
    )
    op.create_table(
        "policy_rollout",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), sa.ForeignKey("policy_bundle.id"), nullable=False),
        sa.Column("cohort_percent", sa.Integer(), nullable=False),
        sa.Column("salt", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rollout_tenant_state", "policy_rollout", ["tenant_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_rollout_tenant_state", table_name="policy_rollout")
    op.drop_table("policy_rollout")
    op.drop_table("policy_bundle")
