"""Persist the permit identity for each applied simulator effect."""

from alembic import op
import sqlalchemy as sa

revision = "0021_effect_identity"
down_revision = "0020_model_token_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows stay queryable for reconciliation. Legacy replays fail
    # closed because their permit binding cannot be proved.
    op.add_column(
        "simulator_applied_effect",
        sa.Column("permit_digest", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("simulator_applied_effect", "permit_digest")
