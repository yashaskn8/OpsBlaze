"""Additive Stage 3 migration for Features B2, B3, and E8.

Revision ID: stage3_002_b2_b3_e8
Revises: b3_005_claim_payload
Create Date: 2026-11-07 21:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage3_002_b2_b3_e8"
down_revision: str | None = "b3_005_claim_payload"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # 1. Create adapter_conformance table for E8 if not exists
    if "adapter_conformance" not in existing_tables:
        op.create_table(
            "adapter_conformance",
            sa.Column("adapter_id", sa.String(), nullable=False),
            sa.Column("adapter_version", sa.String(), nullable=False),
            sa.Column("code_digest", sa.String(), nullable=False),
            sa.Column(
                "status",
                sa.String(),
                server_default=sa.text("'READ_ONLY_UNVERIFIED'::character varying"),
                nullable=False,
            ),
            sa.Column(
                "retention_horizon_seconds",
                sa.Integer(),
                server_default=sa.text("300"),
                nullable=False,
            ),
            sa.Column("capabilities_json", sa.JSON(), nullable=False),
            sa.Column("conformance_evidence_json", sa.JSON(), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('READ_ONLY_UNVERIFIED', 'CONFORMANT', 'QUARANTINED', 'REVOKED_DIGEST_MISMATCH', 'EXPIRED')",
                name="chk_adapter_conformance_status_valid",
            ),
            sa.CheckConstraint(
                "retention_horizon_seconds >= 0",
                name="chk_adapter_retention_nonnegative",
            ),
            sa.PrimaryKeyConstraint("adapter_id", name="adapter_conformance_pkey"),
        )

    # 2. Update resource_reservation indexes & check constraint for B2
    if "resource_reservation" in existing_tables:
        res_indexes = {
            idx["name"]
            for idx in inspector.get_indexes("resource_reservation", schema=current_schema)
        }
        if "ux_active_reservation_per_target" in res_indexes:
            op.drop_index("ux_active_reservation_per_target", table_name="resource_reservation")
        op.create_index(
            "ux_active_reservation_per_target",
            "resource_reservation",
            ["tenant_id", "target_id"],
            unique=True,
            postgresql_where=sa.text(
                "(state)::text = ANY ((ARRAY['RESERVED'::character varying, 'DISPATCHED'::character varying])::text[])"
            ),
        )

        if "ix_reservation_active_expiry" in res_indexes:
            op.drop_index("ix_reservation_active_expiry", table_name="resource_reservation")
        op.create_index(
            "ix_reservation_active_expiry",
            "resource_reservation",
            ["expires_at"],
            unique=False,
            postgresql_where=sa.text(
                "(state)::text = ANY ((ARRAY['RESERVED'::character varying, 'DISPATCHED'::character varying])::text[])"
            ),
        )

        res_checks = {
            chk["name"]
            for chk in inspector.get_check_constraints(
                "resource_reservation", schema=current_schema
            )
        }
        if "chk_reservation_state_valid" not in res_checks:
            op.create_check_constraint(
                "chk_reservation_state_valid",
                "resource_reservation",
                "state IN ('RESERVED', 'DISPATCHED', 'RELEASED', 'EXPIRED', 'CANCELLED')",
            )

    # 3. Add additive columns and check constraint to effect_receipt for B3
    if "effect_receipt" in existing_tables:
        eff_cols = {
            col["name"] for col in inspector.get_columns("effect_receipt", schema=current_schema)
        }
        if "adapter_digest" not in eff_cols:
            op.add_column(
                "effect_receipt",
                sa.Column(
                    "adapter_digest",
                    sa.String(),
                    server_default=sa.text("''::character varying"),
                    nullable=False,
                ),
            )
        if "last_error" not in eff_cols:
            op.add_column(
                "effect_receipt",
                sa.Column("last_error", sa.String(), nullable=True),
            )

        eff_checks = {
            chk["name"]
            for chk in inspector.get_check_constraints("effect_receipt", schema=current_schema)
        }
        if "chk_effect_receipt_state_valid" not in eff_checks:
            op.create_check_constraint(
                "chk_effect_receipt_state_valid",
                "effect_receipt",
                "state IN ('PREPARED', 'SENT', 'UNKNOWN', 'CONFIRMED', 'VERIFIED', 'FAILED', 'NEEDS_REVIEW')",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "effect_receipt" in existing_tables:
        eff_checks = {
            chk["name"]
            for chk in inspector.get_check_constraints("effect_receipt", schema=current_schema)
        }
        if "chk_effect_receipt_state_valid" in eff_checks:
            op.drop_constraint("chk_effect_receipt_state_valid", "effect_receipt", type_="check")

        eff_cols = {
            col["name"] for col in inspector.get_columns("effect_receipt", schema=current_schema)
        }
        if "last_error" in eff_cols:
            op.drop_column("effect_receipt", "last_error")
        if "adapter_digest" in eff_cols:
            op.drop_column("effect_receipt", "adapter_digest")

    if "resource_reservation" in existing_tables:
        res_checks = {
            chk["name"]
            for chk in inspector.get_check_constraints(
                "resource_reservation", schema=current_schema
            )
        }
        if "chk_reservation_state_valid" in res_checks:
            op.drop_constraint("chk_reservation_state_valid", "resource_reservation", type_="check")

        res_indexes = {
            idx["name"]
            for idx in inspector.get_indexes("resource_reservation", schema=current_schema)
        }
        if "ux_active_reservation_per_target" in res_indexes:
            op.drop_index("ux_active_reservation_per_target", table_name="resource_reservation")
        op.create_index(
            "ux_active_reservation_per_target",
            "resource_reservation",
            ["tenant_id", "target_id"],
            unique=True,
            postgresql_where=sa.text("(state)::text = 'RESERVED'::text"),
        )

        if "ix_reservation_active_expiry" in res_indexes:
            op.drop_index("ix_reservation_active_expiry", table_name="resource_reservation")
        op.create_index(
            "ix_reservation_active_expiry",
            "resource_reservation",
            ["expires_at"],
            unique=False,
            postgresql_where=sa.text("(state)::text = 'RESERVED'::text"),
        )

    if "adapter_conformance" in existing_tables:
        op.drop_table("adapter_conformance")
