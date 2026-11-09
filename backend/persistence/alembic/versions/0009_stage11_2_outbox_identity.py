"""Stage 11.2 migration: Outbox deduplication & payload, IdentityMembership table.

Revision ID: stage11_2_001_outbox_identity
Revises: stage11_001_e6
Create Date: 2026-11-09 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage11_2_001_outbox_identity"
down_revision: str | None = "stage11_001_e6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # -----------------------------------------------------------------------
    # 1. Update `outbox` table with dedupe_key, payload, last_error, lease_owner
    # -----------------------------------------------------------------------
    if "outbox" in existing_tables:
        existing_cols = {
            col["name"] for col in inspector.get_columns("outbox", schema=current_schema)
        }

        # Alter incident_id to nullable=True
        op.alter_column(
            "outbox",
            "incident_id",
            existing_type=sa.Uuid(as_uuid=True),
            nullable=True,
        )

        # Alter kind to String(64)
        op.alter_column(
            "outbox",
            "kind",
            existing_type=sa.String(),
            type_=sa.String(64),
            nullable=False,
        )

        # A. Add payload (JSON NOT NULL default '{}')
        if "payload" not in existing_cols:
            op.add_column(
                "outbox",
                sa.Column(
                    "payload",
                    sa.JSON(),
                    server_default=sa.text("'{}'::json"),
                    nullable=False,
                ),
            )

        # B. Add last_error (VARCHAR(2048) nullable)
        if "last_error" not in existing_cols:
            op.add_column(
                "outbox",
                sa.Column(
                    "last_error",
                    sa.String(2048),
                    nullable=True,
                ),
            )

        # C. Add lease_owner (VARCHAR(128) nullable)
        if "lease_owner" not in existing_cols:
            op.add_column(
                "outbox",
                sa.Column(
                    "lease_owner",
                    sa.String(128),
                    nullable=True,
                ),
            )

        # D. Add dedupe_key (VARCHAR(128) nullable)
        if "dedupe_key" not in existing_cols:
            op.add_column(
                "outbox",
                sa.Column("dedupe_key", sa.String(128), nullable=True),
            )

        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("outbox", schema=current_schema)
        }

        # E. Add index ix_outbox_kind_pending
        if "ix_outbox_kind_pending" not in existing_indexes:
            op.create_index(
                "ix_outbox_kind_pending",
                "outbox",
                ["kind", "available_at", "id"],
                postgresql_where=sa.text("delivered_at IS NULL"),
            )

        # F. Add partial unique index uq_outbox_dedupe_in_flight
        if "uq_outbox_dedupe_in_flight" not in existing_indexes:
            op.create_index(
                "uq_outbox_dedupe_in_flight",
                "outbox",
                ["kind", "dedupe_key"],
                unique=True,
                postgresql_where=sa.text("delivered_at IS NULL AND dedupe_key IS NOT NULL"),
            )

    # -----------------------------------------------------------------------
    # 2. Create `identity_membership` table
    # -----------------------------------------------------------------------
    if "identity_membership" not in existing_tables:
        op.create_table(
            "identity_membership",
            sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("idp_issuer", sa.String(256), nullable=False),
            sa.Column("idp_subject", sa.String(256), nullable=False),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column(
                "actor_kind",
                sa.String(32),
                server_default=sa.text("'HUMAN'"),
                nullable=False,
            ),
            sa.Column(
                "roles",
                sa.JSON(),
                server_default=sa.text("'[]'::json"),
                nullable=False,
            ),
            sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deactivated_by", sa.String(128), nullable=True),
            sa.Column("deactivation_reason", sa.String(512), nullable=True),
            sa.UniqueConstraint(
                "tenant_id",
                "idp_issuer",
                "idp_subject",
                name="uq_identity_membership_tenant_idp",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "actor_id",
                name="uq_identity_membership_tenant_actor",
            ),
            sa.CheckConstraint(
                "actor_kind IN ('HUMAN', 'SYSTEM', 'BOT')",
                name="chk_membership_actor_kind",
            ),
        )
        op.create_index(
            "idx_identity_membership_lookup",
            "identity_membership",
            ["tenant_id", "idp_issuer", "idp_subject", "is_active"],
        )
        op.create_index(
            "idx_identity_membership_tenant_actor",
            "identity_membership",
            ["tenant_id", "actor_id", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "identity_membership" in existing_tables:
        op.drop_index("idx_identity_membership_tenant_actor", table_name="identity_membership")
        op.drop_index("idx_identity_membership_lookup", table_name="identity_membership")
        op.drop_table("identity_membership")

    if "outbox" in existing_tables:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("outbox", schema=current_schema)
        }
        if "uq_outbox_dedupe_in_flight" in existing_indexes:
            op.drop_index("uq_outbox_dedupe_in_flight", table_name="outbox")
        if "ix_outbox_kind_pending" in existing_indexes:
            op.drop_index("ix_outbox_kind_pending", table_name="outbox")

        existing_cols = {
            col["name"] for col in inspector.get_columns("outbox", schema=current_schema)
        }
        if "dedupe_key" in existing_cols:
            op.drop_column("outbox", "dedupe_key")
        if "lease_owner" in existing_cols:
            op.drop_column("outbox", "lease_owner")
        if "last_error" in existing_cols:
            op.drop_column("outbox", "last_error")
        if "payload" in existing_cols:
            op.drop_column("outbox", "payload")
