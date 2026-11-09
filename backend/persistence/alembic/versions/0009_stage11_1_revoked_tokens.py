"""Stage 11.1 — durable PostgreSQL token revocation.

Revision ID: stage11_1_001_revoked_tokens
Revises: stage11_001_e6
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage11_1_001_revoked_tokens"
down_revision: str | None = "stage11_001_e6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    existing_tables = set(inspector.get_table_names(schema=current_schema))

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

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("revoked_token", schema=current_schema)}
    if "ix_revoked_token_expiry" not in existing_indexes:
        op.create_index("ix_revoked_token_expiry", "revoked_token", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_revoked_token_expiry", table_name="revoked_token")
    op.drop_table("revoked_token")
