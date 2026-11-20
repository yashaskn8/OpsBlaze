"""B4 immutable outbox enqueue age, independent from retry availability."""

from alembic import op
import sqlalchemy as sa

revision = "0022_outbox_age"
down_revision = "0021_effect_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_outbox_age_pending", "outbox", ["created_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_age_pending", table_name="outbox")
    op.drop_column("outbox", "created_at")
