"""Add the durable cursor for bounded lineage recovery."""

from alembic import op
import sqlalchemy as sa

revision = "0048_lineage_recovery"
down_revision = "0047_review_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    checkpoint = op.create_table(
        "lineage_recovery_checkpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Uuid()),
        sa.Column("phase", sa.String(16)),
        sa.Column("source_cursor", sa.Uuid()),
        sa.Column("edge_cursor", sa.JSON()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_lineage_recovery_singleton"),
    )
    op.bulk_insert(checkpoint, [{"id": 1}])


def downgrade() -> None:
    op.drop_table("lineage_recovery_checkpoint")
