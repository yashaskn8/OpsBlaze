"""Reconstructed baseline schema matching existing PostgreSQL revision b3_005_claim_payload.

Revision ID: b3_005_claim_payload
Revises: None
Create Date: 2026-11-04 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b3_005_claim_payload"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    if "tenant" not in existing_tables:
        op.create_table(
            "tenant",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name="tenant_pkey"),
            sa.UniqueConstraint("name", name="tenant_name_key"),
        )

    if "incident" not in existing_tables:
        op.create_table(
            "incident",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("client_key", sa.String(), nullable=False),
            sa.Column("request_sha256", sa.String(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("status_projection", sa.String(), nullable=False),
            sa.Column("status_version", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenant.id"], name="incident_tenant_id_fkey", ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id", name="incident_pkey"),
            sa.UniqueConstraint("tenant_id", "client_key", name="ux_incident_key"),
            sa.UniqueConstraint("tenant_id", "id", name="ux_incident_tenant_id"),
        )
        op.create_index(
            "ix_incident_target_status",
            "incident",
            ["tenant_id", "target_id", "status_projection"],
            unique=False,
        )
        op.create_index(
            "ix_incident_tenant_history",
            "incident",
            ["tenant_id", sa.text("created_at DESC"), "id"],
            unique=False,
        )
        op.create_index("ix_incident_tenant_id", "incident", ["tenant_id"], unique=False)

    if "outbox" not in existing_tables:
        op.create_table(
            "outbox",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("incident_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column(
                "available_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("attempt >= 0", name="chk_outbox_attempt_nonnegative"),
            sa.ForeignKeyConstraint(
                ["incident_id"], ["incident.id"], name="outbox_incident_id_fkey", ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id", name="outbox_pkey"),
        )
        op.create_index("ix_outbox_incident_id", "outbox", ["incident_id"], unique=False)
        op.create_index(
            "ix_outbox_pending",
            "outbox",
            ["available_at", "id"],
            unique=False,
            postgresql_where=sa.text("delivered_at IS NULL"),
        )

    if "resource_epoch" not in existing_tables:
        op.create_table(
            "resource_epoch",
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column(
                "latest_fence",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("version_seen", sa.String(), nullable=True),
            sa.CheckConstraint("latest_fence >= 0", name="chk_epoch_fence_nonnegative"),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenant.id"],
                name="resource_epoch_tenant_id_fkey",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("tenant_id", "target_id", name="resource_epoch_pkey"),
        )

    if "resource_reservation" not in existing_tables:
        op.create_table(
            "resource_reservation",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("holder_incident_id", sa.Uuid(), nullable=False),
            sa.Column("action_digest", sa.String(), nullable=False),
            sa.Column("fence", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "state",
                sa.String(),
                server_default=sa.text("'RESERVED'::character varying"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint("fence > 0", name="chk_reservation_fence_positive"),
            sa.ForeignKeyConstraint(
                ["holder_incident_id"],
                ["incident.id"],
                name="resource_reservation_holder_incident_id_fkey",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "holder_incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_reservation_incident_tenant",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenant.id"],
                name="resource_reservation_tenant_id_fkey",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="resource_reservation_pkey"),
        )
        op.create_index(
            "ux_active_reservation_per_target",
            "resource_reservation",
            ["tenant_id", "target_id"],
            unique=True,
            postgresql_where=sa.text("(state)::text = 'RESERVED'::text"),
        )
        op.create_index(
            "ix_reservation_active_expiry",
            "resource_reservation",
            ["expires_at"],
            unique=False,
            postgresql_where=sa.text("(state)::text = 'RESERVED'::text"),
        )
        op.create_index(
            "ix_reservation_tenant_target_fence",
            "resource_reservation",
            ["tenant_id", "target_id", "fence"],
            unique=False,
        )

    if "effect_receipt" not in existing_tables:
        op.create_table(
            "effect_receipt",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("incident_id", sa.Uuid(), nullable=False),
            sa.Column("plan_digest", sa.String(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("adapter_id", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("fence", sa.BigInteger(), nullable=False),
            sa.Column("expected_version", sa.String(), nullable=False),
            sa.Column(
                "state",
                sa.String(),
                server_default=sa.text("'PREPARED'::character varying"),
                nullable=False,
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("external_ref", sa.String(), nullable=True),
            sa.Column("observed_version", sa.String(), nullable=True),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "action_digest",
                sa.String(),
                server_default=sa.text("''::character varying"),
                nullable=False,
            ),
            sa.Column("claim_token", sa.Uuid(), nullable=True),
            sa.Column("action_payload", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(
                ["incident_id"],
                ["incident.id"],
                name="effect_receipt_incident_id_fkey",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "incident_id"],
                ["incident.tenant_id", "incident.id"],
                name="fk_receipt_incident_tenant",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenant.id"],
                name="effect_receipt_tenant_id_fkey",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="effect_receipt_pkey"),
            sa.UniqueConstraint(
                "tenant_id", "idempotency_key", name="ux_effect_tenant_idempotency"
            ),
        )
        op.create_index(
            "ix_effect_receipt_pending_recon",
            "effect_receipt",
            ["state", "updated_at"],
            unique=False,
            postgresql_where=sa.text(
                "(state)::text = ANY ((ARRAY['UNKNOWN'::character varying, 'SENT'::character varying])::text[])"
            ),
        )
        op.create_index(
            "ix_effect_receipt_history",
            "effect_receipt",
            ["tenant_id", "incident_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("effect_receipt")
    op.drop_table("resource_reservation")
    op.drop_table("resource_epoch")
    op.drop_table("outbox")
    op.drop_table("incident")
    op.drop_table("tenant")
