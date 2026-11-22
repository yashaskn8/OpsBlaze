"""Bind durable receipt identities to the exact original simulator permit.

Legacy receipts remain readable but their NULL binding cannot be used for
verification or new dispatch. No external capability is activated.
"""

from alembic import op
import sqlalchemy as sa

revision = "0038_receipt_permit_binding"
down_revision = "0037_projector_source_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "effect_receipt",
        sa.Column("permit_digest", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("effect_receipt", "permit_digest")
