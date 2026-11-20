
"""E5 immutable frozen paired simulator smoke manifests and gate records."""

from alembic import op
import sqlalchemy as sa

revision = "0010_evaluation_smoke"
down_revision = "0009_operational_lessons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_manifest",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("improvement_candidate.id"), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("candidate_digest", sa.String(64), nullable=False),
        sa.Column("incumbent_id", sa.String(64), nullable=False),
        sa.Column("fixture_digest", sa.String(64), nullable=False),
        sa.Column("oracle_version", sa.String(32), nullable=False),
        sa.Column("trial_budget", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "candidate_id", "candidate_version", "fixture_digest", name="uq_eval_frozen"),
    )
    op.create_index("ix_evaluation_candidate", "evaluation_manifest", ["tenant_id", "candidate_id", "created_at"])
    op.create_table(
        "evaluation_trial",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("manifest_id", sa.Uuid(), sa.ForeignKey("evaluation_manifest.id"), nullable=False),
        sa.Column("pair_id", sa.String(48), nullable=False),
        sa.Column("arm", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("oracle_correct", sa.Boolean(), nullable=False),
        sa.Column("unsafe", sa.Boolean(), nullable=False),
        sa.Column("probe_cost_units", sa.Integer(), nullable=False),
        sa.UniqueConstraint("manifest_id", "pair_id", "arm", name="uq_evaluation_pair_arm"),
    )
    op.create_table(
        "evaluation_gate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("manifest_id", sa.Uuid(), sa.ForeignKey("evaluation_manifest.id"), nullable=False),
        sa.Column("rule_id", sa.String(48), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.String(256), nullable=False),
        sa.UniqueConstraint("manifest_id", "rule_id", name="uq_evaluation_gate"),
    )


def downgrade() -> None:
    op.drop_table("evaluation_gate")
    op.drop_table("evaluation_trial")
    op.drop_index("ix_evaluation_candidate", table_name="evaluation_manifest")
    op.drop_table("evaluation_manifest")
