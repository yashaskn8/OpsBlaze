
"""E6 immutable baseline policy assignment at incident admission."""

from alembic import op
import sqlalchemy as sa

revision = "0019_policy_pin"
down_revision = "0018_decision_assessment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_assignment",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incident.id"), primary_key=True),
        sa.Column("policy_id", sa.String(64), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), sa.ForeignKey("policy_bundle.id"), nullable=True),
        sa.Column("rollout_id", sa.Uuid(), sa.ForeignKey("policy_rollout.id"), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_policy_assignment_policy", "policy_assignment",
        ["tenant_id", "policy_id"],
    )
    # Existing incidents predate this migration; pin them to the known-good
    # incumbent rather than leaving them with no policy identity.
    op.execute(
        sa.text(
            "INSERT INTO policy_assignment "
            "(tenant_id, incident_id, policy_id, source) "
            "SELECT tenant_id, id, 'simulator-lab-route-v1', 'MIGRATED_INCUMBENT' "
            "FROM incident"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_policy_assignment_policy", table_name="policy_assignment")
    op.drop_table("policy_assignment")
