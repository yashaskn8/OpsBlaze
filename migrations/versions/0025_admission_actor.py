"""Bind incident admission to a verified actor pseudonym.

Legacy rows remain nullable: no fabricated human identity during backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_admission_actor"
down_revision = "0024_review_epoch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident", sa.Column("created_by", sa.String(48), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("incident", "created_by")
