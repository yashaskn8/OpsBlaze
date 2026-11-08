"""Stage 9 migration for Feature E5 Candidate-to-Incumbent Regression Gate.

Revision ID: stage9_001_e5
Revises: stage8_001_e3_e4
Create Date: 2026-11-08 16:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage9_001_e5"
down_revision: str | None = "stage8_001_e3_e4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. candidate_evaluation_manifest
    if "candidate_evaluation_manifest" not in existing_tables:
        op.create_table(
            "candidate_evaluation_manifest",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("manifest_sha256", sa.String(64), unique=True, nullable=False),
            sa.Column("incumbent_bundle", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("incumbent_digest", sa.String(64), nullable=False),
            sa.Column("candidate_bundle", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("candidate_digest", sa.String(64), nullable=False),
            sa.Column("candidate_provenance_type", sa.String(32), nullable=False),
            sa.Column("source_proposal_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("source_proposal_version", sa.Integer(), nullable=True),
            sa.Column(
                "source_lesson_ids",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=True,
            ),
            sa.Column("model_id", sa.String(64), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("tool_catalog_version", sa.String(64), nullable=False),
            sa.Column("a2_grant_version", sa.String(64), nullable=False),
            sa.Column("fixture_family", sa.String(128), nullable=False),
            sa.Column("fixture_set_digest", sa.String(64), nullable=False),
            sa.Column("holdout_set_digest", sa.String(64), nullable=False),
            sa.Column("oracle_version", sa.String(64), nullable=False),
            sa.Column("adjudication_rule_version", sa.String(64), nullable=False),
            sa.Column(
                "source_outcome_revision_ids",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column("metric_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column(
                "primary_endpoint",
                sa.String(64),
                server_default=sa.text("'verified_recovery'"),
                nullable=False,
            ),
            sa.Column("sample_size_plan", sa.Integer(), nullable=False),
            sa.Column("random_seed", sa.Integer(), nullable=False),
            sa.Column("budget_ceiling_micros", sa.BigInteger(), nullable=False),
            sa.Column("max_tokens_ceiling", sa.Integer(), nullable=False),
            sa.Column(
                "lifecycle_state",
                sa.String(32),
                server_default=sa.text("'REGISTERED'"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "id", name="uq_candidate_eval_manifest_tenant"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "source_proposal_id"],
                ["improvement_proposal.tenant_id", "improvement_proposal.id"],
                name="fk_manifest_proposal",
                ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "lifecycle_state IN ('REGISTERED', 'FROZEN', 'RUNNING', 'ANALYZING', 'COMPLETED', 'INVALIDATED', 'CANCELLED')",
                name="chk_candidate_eval_manifest_state",
            ),
            sa.CheckConstraint(
                "candidate_provenance_type IN ('E3_PROPOSAL', 'E4_LESSON_SET', 'COMBINED_OFFLINE_STUDY')",
                name="chk_candidate_eval_provenance",
            ),
        )
        op.create_index(
            "ix_candidate_eval_manifest_tenant",
            "candidate_evaluation_manifest",
            ["tenant_id", "lifecycle_state"],
        )
        op.create_index(
            "ix_candidate_eval_manifest_outcome_ids",
            "candidate_evaluation_manifest",
            ["source_outcome_revision_ids"],
            postgresql_using="gin",
        )

    # 2. candidate_evaluation_trial
    if "candidate_evaluation_trial" not in existing_tables:
        op.create_table(
            "candidate_evaluation_trial",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("manifest_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("pair_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("arm", sa.String(32), nullable=False),
            sa.Column("fixture_id", sa.String(128), nullable=False),
            sa.Column(
                "is_negative_control",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column(
                "trial_status",
                sa.String(32),
                server_default=sa.text("'PENDING'"),
                nullable=False,
            ),
            sa.Column("outcome_label", sa.String(64), nullable=True),
            sa.Column("decision_correct", sa.Boolean(), nullable=True),
            sa.Column("probes_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("tokens_consumed", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("cost_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("duration_ms", sa.Float(), server_default=sa.text("0.0"), nullable=False),
            sa.Column(
                "validity_state",
                sa.String(32),
                server_default=sa.text("'VALID'"),
                nullable=False,
            ),
            sa.Column("validity_reason", sa.Text(), nullable=True),
            sa.Column(
                "unsafe_action_detected",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column(
                "unsafe_action_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("safety_violation_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("worker_id", sa.String(128), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_artifact_digest", sa.String(64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["tenant_id", "manifest_id"],
                ["candidate_evaluation_manifest.tenant_id", "candidate_evaluation_manifest.id"],
                name="fk_candidate_eval_trial_manifest",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("manifest_id", "pair_id", "arm", name="uq_candidate_eval_trial_pair_arm"),
            sa.CheckConstraint("arm IN ('INCUMBENT', 'CANDIDATE')", name="chk_candidate_eval_trial_arm"),
            sa.CheckConstraint(
                "trial_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'INVALID')",
                name="chk_candidate_eval_trial_status",
            ),
            sa.CheckConstraint(
                "validity_state IN ('VALID', 'INVALID_TIMEOUT', 'INVALID_ORACLE_OUTAGE', 'INVALID_FIXTURE_CORRUPT')",
                name="chk_candidate_eval_trial_validity",
            ),
        )
        op.create_index(
            "ix_candidate_eval_trial_lookup",
            "candidate_evaluation_trial",
            ["tenant_id", "manifest_id", "arm"],
        )
        op.create_index(
            "ix_candidate_eval_trial_pending",
            "candidate_evaluation_trial",
            ["manifest_id", "trial_status"],
            postgresql_where=sa.text("trial_status IN ('PENDING', 'RUNNING')"),
        )

    # 3. candidate_evaluation_report
    if "candidate_evaluation_report" not in existing_tables:
        op.create_table(
            "candidate_evaluation_report",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("manifest_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("report_status", sa.String(32), nullable=False),
            sa.Column(
                "promotion_eligible",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("invalidation_reason", sa.Text(), nullable=True),
            sa.Column("total_pairs_planned", sa.Integer(), nullable=False),
            sa.Column("total_pairs_completed", sa.Integer(), nullable=False),
            sa.Column(
                "invalid_pairs_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("metrics_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("mcnemar_statistic", sa.Float(), nullable=True),
            sa.Column("mcnemar_p_value", sa.Float(), nullable=True),
            sa.Column("recovery_rate_incumbent", sa.Float(), nullable=False),
            sa.Column("recovery_rate_candidate", sa.Float(), nullable=False),
            sa.Column("recovery_delta", sa.Float(), nullable=False),
            sa.Column("recovery_ci_low", sa.Float(), nullable=False),
            sa.Column("recovery_ci_high", sa.Float(), nullable=False),
            sa.Column("cost_per_verified_incumbent", sa.Float(), nullable=False),
            sa.Column("cost_per_verified_candidate", sa.Float(), nullable=True),
            sa.Column("negative_control_pass_rate", sa.Float(), nullable=False),
            sa.Column(
                "unsafe_action_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("gate_verdicts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("reviewer_id", sa.String(128), nullable=True),
            sa.Column("reviewer_role", sa.String(64), nullable=True),
            sa.Column("review_decision", sa.String(32), nullable=True),
            sa.Column("review_comments", sa.Text(), nullable=True),
            sa.Column(
                "source_eligibility_rechecked",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "evaluated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "superseded_by_report_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("candidate_evaluation_report.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.UniqueConstraint("tenant_id", "id", name="uq_candidate_eval_report_tenant"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "manifest_id"],
                ["candidate_evaluation_manifest.tenant_id", "candidate_evaluation_manifest.id"],
                name="fk_candidate_eval_report_manifest",
                ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "report_status IN ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE')",
                name="chk_candidate_eval_report_status",
            ),
            sa.CheckConstraint(
                "review_decision IS NULL OR review_decision IN ('ACCEPTED', 'REJECTED', 'INCONCLUSIVE_ACKNOWLEDGED')",
                name="chk_candidate_eval_review_decision",
            ),
            sa.UniqueConstraint("manifest_id", "version", name="uq_candidate_eval_report_version"),
        )
        op.create_index(
            "ix_candidate_eval_report_manifest",
            "candidate_evaluation_report",
            ["tenant_id", "manifest_id", "report_status"],
        )


def downgrade() -> None:
    op.drop_index("ix_candidate_eval_report_manifest", table_name="candidate_evaluation_report")
    op.drop_table("candidate_evaluation_report")

    op.drop_index("ix_candidate_eval_trial_pending", table_name="candidate_evaluation_trial")
    op.drop_index("ix_candidate_eval_trial_lookup", table_name="candidate_evaluation_trial")
    op.drop_table("candidate_evaluation_trial")

    op.drop_index("ix_candidate_eval_manifest_outcome_ids", table_name="candidate_evaluation_manifest")
    op.drop_index("ix_candidate_eval_manifest_tenant", table_name="candidate_evaluation_manifest")
    op.drop_table("candidate_evaluation_manifest")
