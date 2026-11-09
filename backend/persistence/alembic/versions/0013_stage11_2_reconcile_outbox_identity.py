"""Stage 11.2 forward reconciliation: RevokedToken table, Outbox indexes, IdentityMembership schema.

Revision ID: stage11_2_003_reconcile
Revises: stage11_2_001_outbox_identity
Create Date: 2026-11-09 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage11_2_003_reconcile"
down_revision: str | None = "stage11_2_001_outbox_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

    # -----------------------------------------------------------------------
    # 1. Reconcile `revoked_token` table
    # -----------------------------------------------------------------------
    if "revoked_token" not in existing_tables:
        op.create_table(
            "revoked_token",
            sa.Column("jti", sa.String(length=128), primary_key=True),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column(
                "tenant_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("tenant.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "revoked_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    existing_rev_indexes = {
        idx["name"]
        for idx in inspector.get_indexes("revoked_token", schema=current_schema)
    } if "revoked_token" in existing_tables or "revoked_token" not in existing_tables else set()
    
    # Re-inspect if newly created
    inspector = sa.inspect(bind)
    existing_rev_indexes = {
        idx["name"] for idx in inspector.get_indexes("revoked_token", schema=current_schema)
    }
    if "ix_revoked_token_expiry" not in existing_rev_indexes:
        op.create_index("ix_revoked_token_expiry", "revoked_token", ["expires_at"])

    # -----------------------------------------------------------------------
    # 2. Reconcile `outbox` table
    # -----------------------------------------------------------------------
    if "outbox" in existing_tables:
        outbox_constraints = {
            c["name"] for c in inspector.get_unique_constraints("outbox", schema=current_schema)
        }
        if "uq_outbox_dedupe_key" in outbox_constraints:
            op.drop_constraint("uq_outbox_dedupe_key", "outbox", type_="unique")

        outbox_indexes = {
            idx["name"] for idx in inspector.get_indexes("outbox", schema=current_schema)
        }
        if "uq_outbox_dedupe_in_flight" not in outbox_indexes:
            op.create_index(
                "uq_outbox_dedupe_in_flight",
                "outbox",
                ["kind", "dedupe_key"],
                unique=True,
                postgresql_where=sa.text("delivered_at IS NULL AND dedupe_key IS NOT NULL"),
            )
        if "ix_outbox_kind_pending" not in outbox_indexes:
            op.create_index(
                "ix_outbox_kind_pending",
                "outbox",
                ["kind", "available_at", "id"],
                postgresql_where=sa.text("delivered_at IS NULL"),
            )

    # -----------------------------------------------------------------------
    # 3. Reconcile `identity_membership` table
    # -----------------------------------------------------------------------
    if "identity_membership" in existing_tables:
        membership_cols = {
            col["name"]
            for col in inspector.get_columns("identity_membership", schema=current_schema)
        }

        # Handle column rename if table came from remote migration 0011
        if "issuer" in membership_cols and "idp_issuer" not in membership_cols:
            op.alter_column("identity_membership", "issuer", new_column_name="idp_issuer")
        if "subject" in membership_cols and "idp_subject" not in membership_cols:
            op.alter_column("identity_membership", "subject", new_column_name="idp_subject")

        # Re-inspect columns after rename
        inspector = sa.inspect(bind)
        membership_cols = {
            col["name"]
            for col in inspector.get_columns("identity_membership", schema=current_schema)
        }

        if "updated_at" not in membership_cols:
            op.add_column(
                "identity_membership",
                sa.Column(
                    "updated_at",
                    sa.DateTime(timezone=True),
                    server_default=sa.func.now(),
                    nullable=False,
                ),
            )
        if "deactivated_at" not in membership_cols:
            op.add_column(
                "identity_membership",
                sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "deactivated_by" not in membership_cols:
            op.add_column(
                "identity_membership",
                sa.Column("deactivated_by", sa.String(length=128), nullable=True),
            )
        if "deactivation_reason" not in membership_cols:
            op.add_column(
                "identity_membership",
                sa.Column("deactivation_reason", sa.String(length=512), nullable=True),
            )

        # Drop old constraint if present
        membership_constraints = {
            c["name"]
            for c in inspector.get_unique_constraints("identity_membership", schema=current_schema)
        }
        if "uq_identity_membership_subject_tenant" in membership_constraints:
            op.drop_constraint(
                "uq_identity_membership_subject_tenant", "identity_membership", type_="unique"
            )

        if "uq_identity_membership_tenant_idp" not in membership_constraints:
            op.create_unique_constraint(
                "uq_identity_membership_tenant_idp",
                "identity_membership",
                ["tenant_id", "idp_issuer", "idp_subject"],
            )
        if "uq_identity_membership_tenant_actor" not in membership_constraints:
            op.create_unique_constraint(
                "uq_identity_membership_tenant_actor",
                "identity_membership",
                ["tenant_id", "actor_id"],
            )

        membership_indexes = {
            idx["name"]
            for idx in inspector.get_indexes("identity_membership", schema=current_schema)
        }
        if "ix_identity_membership_lookup" in membership_indexes:
            op.drop_index("ix_identity_membership_lookup", table_name="identity_membership")

        if "idx_identity_membership_lookup" not in membership_indexes:
            op.create_index(
                "idx_identity_membership_lookup",
                "identity_membership",
                ["tenant_id", "idp_issuer", "idp_subject", "is_active"],
            )
        if "idx_identity_membership_tenant_actor" not in membership_indexes:
            op.create_index(
                "idx_identity_membership_tenant_actor",
                "identity_membership",
                ["tenant_id", "actor_id", "is_active"],
            )


def downgrade() -> None:
    pass
