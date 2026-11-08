"""Stage 5 migration for Features A1, A2, A3, A4, and B4 budget foundation.

Revision ID: stage5_001_a1_a2_a3_a4_b4
Revises: stage3_002_b2_b3_e8
Create Date: 2026-11-07 23:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "stage5_001_a1_a2_a3_a4_b4"
down_revision: str | None = "stage3_002_b2_b3_e8"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. tool_catalog table
    if "tool_catalog" not in existing_tables:
        op.create_table(
            "tool_catalog",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("input_schema_hash", sa.String(64), nullable=False),
            sa.Column("output_schema_hash", sa.String(64), nullable=False),
            sa.Column("read_only", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("max_cost_micros", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("egress_allowlist", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", "version", name="pk_tool_catalog"),
        )

    # 2. capability_grant table
    if "capability_grant" not in existing_tables:
        op.create_table(
            "capability_grant",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("incident_id", sa.UUID(), nullable=False),
            sa.Column("tool_id", sa.String(64), nullable=False),
            sa.Column("tool_version", sa.String(32), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("target_id", sa.String(64), nullable=False),
            sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_capability_grant"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_capability_grant_incident",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tool_id", "tool_version"],
                ["tool_catalog.id", "tool_catalog.version"],
                name="fk_capability_grant_tool",
                ondelete="RESTRICT",
            ),
        )
        op.create_index(
            "ix_capability_grant_lookup",
            "capability_grant",
            ["tenant_id", "incident_id", "tool_id", "target_id"],
        )

    # 3. tool_call table
    if "tool_call" not in existing_tables:
        op.create_table(
            "tool_call",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("incident_id", sa.UUID(), nullable=False),
            sa.Column("step_no", sa.Integer(), nullable=False),
            sa.Column("grant_id", sa.UUID(), nullable=False),
            sa.Column("tool_id", sa.String(64), nullable=False),
            sa.Column("tool_version", sa.String(32), server_default=sa.text("'v1'"), nullable=False),
            sa.Column("target_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("args_digest", sa.String(64), nullable=False),
            sa.Column("args_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("output_digest", sa.String(64), nullable=True),
            sa.Column("result_evidence_id", sa.UUID(), nullable=True),
            sa.Column("duration_ms", sa.Float(), server_default=sa.text("0.0"), nullable=False),
            sa.Column("error_detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_tool_call"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_tool_call_incident",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["grant_id"],
                ["capability_grant.id"],
                name="fk_tool_call_grant",
                ondelete="RESTRICT",
            ),
        )
        op.create_index("ix_tool_call_incident", "tool_call", ["tenant_id", "incident_id", "step_no"])

    # 4. evidence_ledger table
    if "evidence_ledger" not in existing_tables:
        op.create_table(
            "evidence_ledger",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("incident_id", sa.UUID(), nullable=False),
            sa.Column("tool_call_id", sa.UUID(), nullable=True),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("source_version", sa.String(32), nullable=False),
            sa.Column("target_id", sa.String(64), nullable=False),
            sa.Column("target_version", sa.String(64), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("sha256_digest", sa.String(64), nullable=False),
            sa.Column("raw_payload_redacted", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("classification", sa.String(32), nullable=False),
            sa.Column("validity_status", sa.String(32), server_default=sa.text("'VALID'"), nullable=False),
            sa.Column("supersedes_id", sa.UUID(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_evidence_ledger"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_evidence_incident",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tool_call_id"],
                ["tool_call.id"],
                name="fk_evidence_tool_call",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_id"],
                ["evidence_ledger.id"],
                name="fk_evidence_supersedes",
                ondelete="SET NULL",
            ),
        )
        op.create_index("ix_evidence_lookup", "evidence_ledger", ["tenant_id", "incident_id", "validity_status"])

    # 5. investigation_step table
    if "investigation_step" not in existing_tables:
        op.create_table(
            "investigation_step",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("incident_id", sa.UUID(), nullable=False),
            sa.Column("step_no", sa.Integer(), nullable=False),
            sa.Column("loop_state", sa.String(32), nullable=False),
            sa.Column("prompt_digest", sa.String(64), nullable=False),
            sa.Column("evidence_ids_used", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
            sa.Column("token_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("hypotheses_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("selected_probe", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("proposed_plan_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("abstention_reason", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_investigation_step"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_investigation_step_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("tenant_id", "incident_id", "step_no", name="uq_investigation_step"),
        )

    # 6. budget_ledger table
    if "budget_ledger" not in existing_tables:
        op.create_table(
            "budget_ledger",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("tenant_id", sa.UUID(), nullable=False),
            sa.Column("incident_id", sa.UUID(), nullable=False),
            sa.Column("reserved_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("settled_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("reserved_probes", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("settled_probes", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("reserved_cost_micros", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("settled_cost_micros", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("max_tokens", sa.Integer(), server_default=sa.text("50000"), nullable=False),
            sa.Column("max_probes", sa.Integer(), server_default=sa.text("10"), nullable=False),
            sa.Column("max_cost_micros", sa.Integer(), server_default=sa.text("10000000"), nullable=False),
            sa.Column("max_time_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
            sa.Column("reservation_nonce", sa.UUID(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), server_default=sa.text("'ACTIVE'"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_budget_ledger"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_budget_ledger_incident",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("tenant_id", "incident_id", name="uq_budget_ledger_incident"),
        )


def downgrade() -> None:
    op.drop_table("budget_ledger")
    op.drop_table("investigation_step")
    op.drop_table("evidence_ledger")
    op.drop_table("tool_call")
    op.drop_table("capability_grant")
    op.drop_table("tool_catalog")
