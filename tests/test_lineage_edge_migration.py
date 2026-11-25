"""Run actual Alembic revisions against a SQLite schema with existing edges."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

# Historical revision 0043 supports exactly these four kinds, not later additions.
EDGE_SPECS = {
    "hypothesis": ("evidence", "CITES"), "plan": ("evidence", "JUSTIFIED_BY"),
    "approval": ("plan", "REVIEWS"), "outcome": ("effect", "WITNESSES"),
}


def migration(name):
    path = Path(__file__).resolve().parents[1] / 'migrations' / 'versions' / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relation_migration_preserves_existing_rows_and_rejects_wrong_tuples():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))
        conn.execute(sa.text('CREATE TABLE incident (id CHAR(32) PRIMARY KEY)'))
        iid = uuid4().hex
        conn.execute(sa.text('INSERT INTO incident VALUES (:id)'), {'id': iid})
        old = migration('0042_lineage_edge.py')
        new = migration('0043_causal_edge_kinds.py')
        with Operations.context(MigrationContext.configure(conn)):
            old.upgrade()
            def insert(kind, target, relation):
                conn.execute(sa.text('INSERT INTO lineage_edge '
                    '(tenant_id,incident_id,from_type,from_id,to_type,to_id,relation) '
                    'VALUES (:tenant,:incident,:kind,:source,:target,:dest,:rel)'),
                    {'tenant':'lab','incident':iid,'kind':kind,'source':uuid4().hex,
                     'target':target,'dest':uuid4().hex,'rel':relation})
            insert('hypothesis', 'evidence', 'CITES')
            new.upgrade()
            for kind, (target, relation) in EDGE_SPECS.items():
                insert(kind, target, relation)
            for kind, target, relation in [('approval','effect','REVIEWS'),
                                           ('outcome','plan','WITNESSES'),
                                           ('plan','evidence','REVIEWS')]:
                with pytest.raises(sa.exc.IntegrityError):
                    insert(kind, target, relation)
            assert conn.scalar(sa.text('SELECT count(*) FROM lineage_edge')) == 5
            new.downgrade()
            assert conn.scalar(sa.text('SELECT count(*) FROM lineage_edge')) == 3
            assert conn.scalar(sa.text('SELECT count(*) FROM incident')) == 1
            with pytest.raises(sa.exc.IntegrityError):
                insert('approval', 'plan', 'REVIEWS')
            old.downgrade()
    engine.dispose()
