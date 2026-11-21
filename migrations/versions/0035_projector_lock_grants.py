"""Permit row locking without granting a projector rights to business fields.

The marker is a deliberately inert, non-business column. PostgreSQL
requires an UPDATE grant for SELECT ... FOR UPDATE, so the projector's
database role receives UPDATE only on this marker, not status/consent.
"""

from alembic import op
import sqlalchemy as sa

revision = "0035_projector_lock_grants"
down_revision = "0034_experience_redrive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("incident", "experience_consent", "tenant_experience_consent"):
        op.add_column(
            table,
            sa.Column(
                "projector_lock_marker", sa.Integer(),
                nullable=False, server_default="0",
            ),
        )


def downgrade() -> None:
    for table in ("tenant_experience_consent", "experience_consent", "incident"):
        op.drop_column(table, "projector_lock_marker")
