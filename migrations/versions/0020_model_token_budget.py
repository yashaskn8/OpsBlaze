
"""B4 atomic optional advisory token reservations and usage accounting."""

from alembic import op
import sqlalchemy as sa

revision = "0020_model_token_budget"
down_revision = "0019_policy_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_advisory",
        sa.Column("budget_window_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "model_token_window",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("used_tokens", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("model_token_window")
    op.drop_column("model_advisory", "budget_window_start")
