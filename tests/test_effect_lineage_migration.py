"""Exercise effect-relation constraints through real Alembic revisions."""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from test_lineage_edge_migration import migration


def test_effect_relation_upgrade_and_downgrade_keep_prior_edges():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE incident (id CHAR(32) PRIMARY KEY)"))
        incident = uuid4().hex
        conn.execute(sa.text("INSERT INTO incident VALUES (:id)"), {"id": incident})
        with Operations.context(MigrationContext.configure(conn)):
            migration("0042_lineage_edge.py").upgrade()
            migration("0043_causal_edge_kinds.py").upgrade()
            new = migration("0044_effect_lineage.py")

            def insert(source, target, relation):
                conn.execute(sa.text(
                    "INSERT INTO lineage_edge (tenant_id,incident_id,from_type,from_id,to_type,to_id,relation) "
                    "VALUES ('lab',:incident,:source,:sid,:target,:tid,:relation)"
                ), {"incident": incident, "source": source, "sid": uuid4().hex,
                    "target": target, "tid": uuid4().hex, "relation": relation})

            insert("approval", "plan", "REVIEWS")
            new.upgrade()
            for relation in ("PREPARED_FOR", "REFERENCES_PLAN", "AUTHORIZED_BY"):
                insert("effect", "plan", relation)
            insert("effect", "approval", "AUTHORIZED_BY_GRANT")
            for target, relation in (("plan", "AUTHORIZED_BY_GRANT"), ("approval", "AUTHORIZED_BY"),
                                     ("evidence", "CITES")):
                with pytest.raises(sa.exc.IntegrityError):
                    insert("effect", target, relation)
            assert conn.scalar(sa.text("SELECT count(*) FROM lineage_edge")) == 5
            new.downgrade()
            assert conn.scalar(sa.text("SELECT count(*) FROM lineage_edge")) == 1
            assert conn.scalar(sa.text("SELECT count(*) FROM incident")) == 1
    engine.dispose()
