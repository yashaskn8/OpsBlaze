"""Allow bounded historical learning relationships in the P3 projection."""

from alembic import op
import sqlalchemy as sa

revision = "0045_learning_lineage"
down_revision = "0044_effect_lineage"
branch_labels = None
depends_on = None

OLD = (
    "((to_type = 'evidence' AND ((from_type = 'hypothesis' AND relation = 'CITES') "
    "OR (from_type = 'plan' AND relation = 'JUSTIFIED_BY'))) "
    "OR (from_type = 'approval' AND to_type = 'plan' AND relation = 'REVIEWS') "
    "OR (from_type = 'outcome' AND to_type = 'effect' AND relation = 'WITNESSES')) "
    "OR (from_type = 'effect' AND ((to_type = 'plan' AND "
    "relation IN ('PREPARED_FOR', 'REFERENCES_PLAN', 'AUTHORIZED_BY')) "
    "OR (to_type = 'approval' AND relation = 'AUTHORIZED_BY_GRANT')))"
)
NEW = (
    f"({OLD}) OR (from_type = 'experience' AND to_type = 'outcome' AND relation = 'DERIVED_FROM') "
    "OR (from_type = 'lesson' AND to_type = 'experience' AND relation = 'SUPPORTED_BY') "
    "OR (from_type = 'failure' AND to_type = 'outcome' AND relation = 'CLASSIFIES')"
)


def upgrade() -> None:
    with op.batch_alter_table("lineage_edge") as batch:
        batch.drop_constraint("ck_lineage_edge_effect_relation", type_="check")
        batch.create_check_constraint("ck_lineage_edge_learning_relation", NEW)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM lineage_edge WHERE from_type IN ('experience', 'lesson', 'failure')"))
    with op.batch_alter_table("lineage_edge") as batch:
        batch.drop_constraint("ck_lineage_edge_learning_relation", type_="check")
        batch.create_check_constraint("ck_lineage_edge_effect_relation", OLD)
