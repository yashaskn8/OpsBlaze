"""Stage 11.2 — authoritative external identity-to-tenant membership.

Revision ID: stage11_2_002_identity_membership
Revises: stage11_2_001_guardrail_outbox
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage11_2_002_identity_membership"
down_revision: str | None = "stage11_2_001_guardrail_outbox"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "identity_membership",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False, server_default="HUMAN"),
        sa.Column("roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("issuer", "subject", "tenant_id", name="uq_identity_membership_subject_tenant"),
    )
    op.create_index(
        "ix_identity_membership_lookup",
        "identity_membership",
        ["issuer", "subject", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_identity_membership_lookup", table_name="identity_membership")
    op.drop_table("identity_membership")
