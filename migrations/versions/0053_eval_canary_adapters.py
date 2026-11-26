"""S1/E4/E5/E6/E8 experiment resume, lesson applicability, eval pagination, and lab adapter enablement."""

from alembic import op
import sqlalchemy as sa

revision = "0053_eval_canary_adapters"
down_revision = "0052_learning_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "replay_experiment",
        sa.Column("invalidation_reason", sa.String(128), nullable=True),
    )
    op.add_column(
        "replay_experiment",
        sa.Column("workload_version", sa.String(32), nullable=False, server_default="workload-v1"),
    )
    op.add_column(
        "replay_experiment",
        sa.Column("evidence_policy_version", sa.String(32), nullable=False, server_default="causal-v1"),
    )

    op.add_column(
        "operational_lesson",
        sa.Column("target_family", sa.String(64), nullable=False, server_default="simulator"),
    )
    op.add_column(
        "operational_lesson",
        sa.Column("policy_id", sa.String(64), nullable=False, server_default="deterministic-v1"),
    )
    op.add_column(
        "operational_lesson",
        sa.Column("conflict_evidence", sa.JSON(), nullable=False, server_default="{}"),
    )

    op.add_column(
        "evaluation_manifest",
        sa.Column("supersedes_manifest_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "evaluation_manifest",
        sa.Column("correction_reason", sa.String(256), nullable=True),
    )
    op.add_column(
        "evaluation_manifest",
        sa.Column("reviewer_decision", sa.String(24), nullable=True),
    )
    op.add_column(
        "evaluation_manifest",
        sa.Column("reviewer_id", sa.String(48), nullable=True),
    )

    op.create_table(
        "adapter_enablement",
        sa.Column("adapter_id", sa.String(48), primary_key=True),
        sa.Column("scope", sa.String(24), nullable=False, server_default="LAB_ONLY"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("conformance_run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.String(48), nullable=False),
        sa.Column("revocation_reason", sa.String(256), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("adapter_enablement")
    op.drop_column("evaluation_manifest", "reviewer_id")
    op.drop_column("evaluation_manifest", "reviewer_decision")
    op.drop_column("evaluation_manifest", "correction_reason")
    op.drop_column("evaluation_manifest", "supersedes_manifest_id")
    op.drop_column("operational_lesson", "conflict_evidence")
    op.drop_column("operational_lesson", "policy_id")
    op.drop_column("operational_lesson", "target_family")
    op.drop_column("replay_experiment", "evidence_policy_version")
    op.drop_column("replay_experiment", "workload_version")
    op.drop_column("replay_experiment", "invalidation_reason")
