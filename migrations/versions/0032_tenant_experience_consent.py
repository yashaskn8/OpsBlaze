"""E1 tenant consent epoch and bounded cleanup of superseded reuse data."""

from alembic import op
import sqlalchemy as sa

revision = "0032_tenant_experience_consent"
down_revision = "0031_shadow_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_experience_consent",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Epoch zero means legacy incident-level opt-in only. After the first
    # tenant policy update, only matching, current-epoch capsules are reusable.
    op.add_column(
        "experience_capsule",
        sa.Column(
            "tenant_consent_epoch", sa.Integer(), nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_capsule_tenant_epoch",
        "experience_capsule",
        ["tenant_id", "tenant_consent_epoch", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_capsule_tenant_epoch", table_name="experience_capsule")
    op.drop_column("experience_capsule", "tenant_consent_epoch")
    op.drop_table("tenant_experience_consent")
