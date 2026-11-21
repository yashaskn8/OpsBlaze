"""E1 one-time controlled projection redrive with durable source linkage.

Never reopen the terminal dead-letter row. A unique source link makes
concurrent administrative redrive requests one-winner across workers.
"""

from alembic import op
import sqlalchemy as sa

revision = "0034_experience_redrive"
down_revision = "0033_experience_dead_letter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox", sa.Column(
            "redrive_source_id", sa.BigInteger(),
            sa.ForeignKey("outbox.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "outbox", sa.Column("redrive_authorized_by", sa.String(48)),
    )
    op.add_column(
        "outbox", sa.Column("redrive_reason", sa.String(32)),
    )
    op.create_index(
        "uq_outbox_redrive_source",
        "outbox", ["redrive_source_id"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_outbox_redrive_source", table_name="outbox")
    op.drop_column("outbox", "redrive_reason")
    op.drop_column("outbox", "redrive_authorized_by")
    op.drop_column("outbox", "redrive_source_id")
