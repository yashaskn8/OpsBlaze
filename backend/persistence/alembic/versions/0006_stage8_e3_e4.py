"""Stage 8 migration for Features E3 Failure Taxonomy & Proposals and E4 Validated Lessons.

Revision ID: stage8_001_e3_e4
Revises: stage7_001_e1_e2
Create Date: 2026-11-08 14:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage8_001_e3_e4"
down_revision: str | None = "stage7_001_e1_e2"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # Enable btree_gin for multi-column composite GIN indexing
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS btree_gin;"))

    # 1. failure_cluster table
    if "failure_cluster" not in existing_tables:
        op.create_table(
            "failure_cluster",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("signature_hash", sa.String(64), nullable=False),
            sa.Column("failure_origin_code", sa.String(64), nullable=False),
            sa.Column("subsystem_version", sa.String(64), nullable=False),
            sa.Column("first_divergent_step_category", sa.String(64), nullable=False),
            sa.Column("cluster_status", sa.String(32), server_default=sa.text("'ACTIVE'"), nullable=False),
            sa.Column("case_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("exemplar_case_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("quarantine_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "failure_origin_code",
                "first_divergent_step_category",
                "subsystem_version",
                name="uq_failure_cluster_sig",
            ),
            sa.UniqueConstraint("tenant_id", "id", name="uq_failure_cluster_tenant"),
            sa.CheckConstraint(
                "cluster_status IN ('ACTIVE', 'QUARANTINED', 'RESOLVED', 'MERGED')",
                name="chk_failure_cluster_status",
            ),
        )
        op.create_index(
            "ix_failure_cluster_tenant_status",
            "failure_cluster",
            ["tenant_id", "cluster_status"],
        )

    # 2. failure_case table
    if "failure_case" not in existing_tables:
        op.create_table(
            "failure_case",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("outcome_revision_id", sa.Uuid(as_uuid=True), sa.ForeignKey("outcome_revision.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("capsule_id", sa.Uuid(as_uuid=True), sa.ForeignKey("experience_capsule.id", ondelete="SET NULL"), nullable=True),
            sa.Column("cluster_id", sa.Uuid(as_uuid=True), sa.ForeignKey("failure_cluster.id", ondelete="SET NULL"), nullable=True),
            sa.Column("failure_origin_code", sa.String(64), nullable=False),
            sa.Column("subsystem_version", sa.String(64), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("first_divergent_step_category", sa.String(64), nullable=False),
            sa.Column("first_divergent_step", sa.String(128), nullable=True),
            sa.Column("classification_status", sa.String(32), server_default=sa.text("'DETECTED'"), nullable=False),
            sa.Column("classification_version", sa.String(32), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("evidence_digest", sa.String(64), nullable=False),
            sa.Column(
                "details",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_failure_case_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("tenant_id", "outcome_revision_id", name="uq_failure_case_outcome"),
            sa.UniqueConstraint("tenant_id", "id", name="uq_failure_case_tenant"),
            sa.CheckConstraint(
                "classification_status IN ('DETECTED', 'CLASSIFIED', 'UNCERTAIN', 'CLUSTERED', 'IGNORED')",
                name="chk_failure_case_status",
            ),
        )
        op.create_index(
            "ix_failure_case_cluster",
            "failure_case",
            ["tenant_id", "cluster_id"],
        )
        op.create_index(
            "ix_failure_case_tenant_origin",
            "failure_case",
            ["tenant_id", "failure_origin_code"],
        )
        op.create_index(
            "ix_failure_case_incident",
            "failure_case",
            ["tenant_id", "incident_id"],
        )

        # Add FK from failure_cluster.exemplar_case_id to failure_case.id now that failure_case exists
        op.create_foreign_key(
            "fk_cluster_exemplar",
            "failure_cluster",
            "failure_case",
            ["exemplar_case_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 3. improvement_proposal table
    if "improvement_proposal" not in existing_tables:
        op.create_table(
            "improvement_proposal",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("cluster_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("proposal_type", sa.String(64), nullable=False),
            sa.Column("base_policy_version", sa.String(64), nullable=False),
            sa.Column("target_component", sa.String(128), nullable=False),
            sa.Column("structured_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("expected_benefit", sa.Text(), nullable=False),
            sa.Column("risk_assessment", sa.Text(), nullable=False),
            sa.Column("evaluation_plan", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), server_default=sa.text("'DRAFT'"), nullable=False),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("drafter_kind", sa.String(32), server_default=sa.text("'MODEL'"), nullable=False),
            sa.Column("drafter_id", sa.String(128), nullable=False),
            sa.Column("reviewer_id", sa.String(128), nullable=True),
            sa.Column("reviewer_role", sa.String(64), nullable=True),
            sa.Column("review_decision", sa.String(32), nullable=True),
            sa.Column("review_comments", sa.Text(), nullable=True),
            sa.Column("rejection_code", sa.String(64), nullable=True),
            sa.Column("rejection_reason", sa.Text(), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "cluster_id"],
                ["failure_cluster.tenant_id", "failure_cluster.id"],
                name="fk_proposal_cluster",
                ondelete="RESTRICT",
            ),
            sa.UniqueConstraint("tenant_id", "id", name="uq_improvement_proposal_tenant"),
            sa.CheckConstraint(
                "status IN ('DRAFT', 'PROPOSED', 'REVIEWED', 'EVALUATION_READY', 'REJECTED')",
                name="chk_improvement_proposal_status",
            ),
            sa.CheckConstraint(
                "proposal_type IN ('PROMPT_REFINEMENT', 'CONTEXT_WINDOW_BUDGET', 'TOOL_DESCRIPTION')",
                name="chk_improvement_proposal_type",
            ),
        )
        op.create_index(
            "ix_improvement_proposal_lookup",
            "improvement_proposal",
            ["tenant_id", "cluster_id", "status"],
        )

    # 4. operational_lesson table
    if "operational_lesson" not in existing_tables:
        op.create_table(
            "operational_lesson",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("lesson_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("lesson_type", sa.String(32), nullable=False),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("redacted_summary", sa.Text(), nullable=False),
            sa.Column(
                "applicability_predicate",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "search_vector",
                postgresql.TSVECTOR(),
                sa.Computed(
                    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(redacted_summary, ''))",
                    persisted=True,
                ),
                nullable=False,
            ),
            sa.Column("consent_epoch", sa.Integer(), nullable=False),
            sa.Column("confidence_band", sa.String(16), server_default=sa.text("'MEDIUM'"), nullable=False),
            sa.Column("lifecycle_state", sa.String(32), server_default=sa.text("'DRAFT'"), nullable=False),
            sa.Column("valid_from", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.Column("validated_by", sa.String(128), nullable=True),
            sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("quarantine_reason", sa.Text(), nullable=True),
            sa.Column(
                "source_outcome_revision_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("outcome_revision.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "source_capsule_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("experience_capsule.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source_cluster_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("proposal_id", sa.Uuid(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "source_cluster_id"],
                ["failure_cluster.tenant_id", "failure_cluster.id"],
                name="fk_lesson_cluster",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "proposal_id"],
                ["improvement_proposal.tenant_id", "improvement_proposal.id"],
                name="fk_lesson_proposal",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("tenant_id", "lesson_id", "version", name="uq_lesson_tenant_version"),
            sa.CheckConstraint(
                "lifecycle_state IN ('DRAFT', 'SOURCE_CHECKED', 'VALIDATED', 'ACTIVE', 'EXPIRED', 'QUARANTINED', 'REVOKED')",
                name="chk_lesson_lifecycle",
            ),
            sa.CheckConstraint(
                "lesson_type IN ('VERIFIED_RECOVERY', 'FAILURE_PATTERN', 'COUNTEREXAMPLE')",
                name="chk_lesson_type",
            ),
            sa.CheckConstraint(
                "confidence_band IN ('HIGH', 'MEDIUM', 'LOW')",
                name="chk_lesson_confidence",
            ),
        )
        op.create_index(
            "ix_operational_lesson_search",
            "operational_lesson",
            ["tenant_id", "search_vector"],
            postgresql_using="gin",
        )
        op.create_index(
            "ix_operational_lesson_lookup",
            "operational_lesson",
            ["tenant_id", "lifecycle_state", "valid_until"],
        )
        op.create_index(
            "ix_operational_lesson_epoch",
            "operational_lesson",
            ["tenant_id", "consent_epoch"],
        )
        op.create_index(
            "ix_operational_lesson_source_outcome",
            "operational_lesson",
            ["tenant_id", "source_outcome_revision_id"],
        )

    # 5. lesson_usage table
    if "lesson_usage" not in existing_tables:
        op.create_table(
            "lesson_usage",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("incident_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("step_id", sa.Uuid(as_uuid=True), sa.ForeignKey("investigation_step.id", ondelete="SET NULL"), nullable=True),
            sa.Column("lesson_id", sa.Uuid(as_uuid=True), nullable=False),
            sa.Column("lesson_version", sa.Integer(), nullable=False),
            sa.Column("policy_bundle_version", sa.String(64), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("retrieval_digest", sa.String(64), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_lesson_usage_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "incident_id",
                "step_id",
                "lesson_id",
                "lesson_version",
                name="uq_lesson_usage_step",
            ),
        )
        op.create_index(
            "ix_lesson_usage_lookup",
            "lesson_usage",
            ["tenant_id", "lesson_id", "lesson_version"],
        )
        op.create_index(
            "ix_lesson_usage_incident",
            "lesson_usage",
            ["tenant_id", "incident_id"],
        )

    # 6. Additive non-breaking fix for experience_audit (nullable capsule_id)
    if "experience_audit" in existing_tables:
        op.alter_column("experience_audit", "capsule_id", nullable=True)


def downgrade() -> None:
    # Reverse alteration of experience_audit
    op.alter_column("experience_audit", "capsule_id", nullable=False)

    # Drop tables in reverse dependency order
    op.drop_table("lesson_usage")
    op.drop_table("operational_lesson")
    op.drop_table("improvement_proposal")

    # Drop fk_cluster_exemplar before dropping failure_case
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    fks = inspector.get_foreign_keys("failure_cluster", schema=current_schema)
    for fk in fks:
        if fk.get("name") == "fk_cluster_exemplar":
            op.drop_constraint("fk_cluster_exemplar", "failure_cluster", type_="foreignkey")

    op.drop_table("failure_case")
    op.drop_table("failure_cluster")
