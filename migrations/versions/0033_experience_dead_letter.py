"""E1 terminal experience-projection delivery with explicit operator review.

Dead-letter rows are never considered delivered. A fixed code, timestamp
and reviewer pseudonym are retained for diagnosis without storing raw errors.
"""

from alembic import op
import sqlalchemy as sa

revision = "0033_experience_dead_letter"
down_revision = "0032_tenant_experience_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox", sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
    )
    op.add_column("outbox", sa.Column("dead_letter_code", sa.String(32)))
    op.add_column(
        "outbox", sa.Column("dead_letter_reviewed_at", sa.DateTime(timezone=True)),
    )
    op.add_column("outbox", sa.Column("dead_letter_reviewed_by", sa.String(48)))
    op.create_index(
        "ix_outbox_dead_letter", "outbox", ["dead_lettered_at", "id"],
        postgresql_where=sa.text("dead_lettered_at IS NOT NULL"),
    )
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.create_index(
        "ix_outbox_pending", "outbox", ["available_at", "id"],
        postgresql_where=sa.text(
            "delivered_at IS NULL AND dead_lettered_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.create_index(
        "ix_outbox_pending", "outbox", ["available_at", "id"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.drop_index("ix_outbox_dead_letter", table_name="outbox")
    op.drop_column("outbox", "dead_letter_reviewed_by")
    op.drop_column("outbox", "dead_letter_reviewed_at")
    op.drop_column("outbox", "dead_letter_code")
    op.drop_column("outbox", "dead_lettered_at")
