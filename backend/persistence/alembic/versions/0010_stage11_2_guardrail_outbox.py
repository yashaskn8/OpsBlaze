"""Stage 11.2 — durable release guardrail outbox events.

Revision ID: stage11_2_001_guardrail_outbox
Revises: stage11_1_001_revoked_tokens
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "stage11_2_001_guardrail_outbox"
down_revision: str | None = "stage11_1_001_revoked_tokens"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("dedupe_key", sa.String(length=255), nullable=True))
    op.add_column("outbox", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_outbox_dedupe_key", "outbox", ["dedupe_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    current_schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
    tables = set(inspector.get_table_names(schema=current_schema))
    if "outbox" in tables:
        constraints = {c["name"] for c in inspector.get_unique_constraints("outbox", schema=current_schema)}
        if "uq_outbox_dedupe_key" in constraints:
            op.drop_constraint("uq_outbox_dedupe_key", "outbox", type_="unique")
        cols = {col["name"] for col in inspector.get_columns("outbox", schema=current_schema)}
        if "last_error" in cols:
            op.drop_column("outbox", "last_error")
        if "dedupe_key" in cols:
            op.drop_column("outbox", "dedupe_key")
