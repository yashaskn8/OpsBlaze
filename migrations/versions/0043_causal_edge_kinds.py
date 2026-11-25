"""Extend the rebuildable P3 projection to approval and outcome relations."""

from alembic import op
import sqlalchemy as sa

revision = "0043_causal_edge_kinds"
down_revision = "0042_lineage_edge"
branch_labels = None
depends_on = None

OLD = (
    "to_type = 'evidence' AND ((from_type = 'hypothesis' AND relation = 'CITES') "
    "OR (from_type = 'plan' AND relation = 'JUSTIFIED_BY'))"
)
NEW = (
    f"({OLD}) OR (from_type = 'approval' AND to_type = 'plan' AND relation = 'REVIEWS') "
    "OR (from_type = 'outcome' AND to_type = 'effect' AND relation = 'WITNESSES')"
)


def upgrade() -> None:
    with op.batch_alter_table("lineage_edge") as batch:
        batch.drop_constraint("ck_lineage_edge_evidence_relation", type_="check")
        batch.create_check_constraint("ck_lineage_edge_causal_relation", NEW)


def downgrade() -> None:
    # Only rebuildable projection rows are discarded, never source records.
    op.execute(sa.text("DELETE FROM lineage_edge WHERE from_type IN ('approval', 'outcome')"))
    with op.batch_alter_table("lineage_edge") as batch:
        batch.drop_constraint("ck_lineage_edge_causal_relation", type_="check")
        batch.create_check_constraint("ck_lineage_edge_evidence_relation", OLD)
