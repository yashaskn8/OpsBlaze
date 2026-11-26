"""Check the capsule trigger against SQLAlchemy models and both supported databases."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from backend.models import Base, ExperienceCapsule, Incident, LineageEdge, OutcomeRevision
from backend.settings import get_settings
from test_lineage_edge_migration import migration


capsules = ExperienceCapsule.__table__
lineage = LineageEdge.__table__


def check_projection(conn, *, installed):
    revision = migration("0046_experience_edge.py")
    incident_id, other_incident_id = uuid4(), uuid4()
    tenant = f"edge-{uuid4().hex}"
    foreign = f"edge-{uuid4().hex}"
    valid_outcome, old_outcome, wrong_incident, wrong_tenant, owner_outcome, retry_outcome = (
        uuid4() for _ in range(6)
    )
    for source in (incident_id, other_incident_id):
        conn.execute(sa.insert(Incident).values(
            id=source, tenant_id=tenant, client_key=str(source), target_id="synthetic",
            request_sha256="0" * 64, created_by="fixture",
        ))
    for outcome, source, scope, number in (
        (valid_outcome, incident_id, tenant, 1),
        (old_outcome, incident_id, tenant, 2),
        (wrong_incident, other_incident_id, tenant, 1),
        (wrong_tenant, incident_id, foreign, 1),
        (owner_outcome, other_incident_id, tenant, 2),
        (retry_outcome, incident_id, tenant, 3),
    ):
        conn.execute(sa.insert(OutcomeRevision).values(
            id=outcome, incident_id=source, tenant_id=scope, revision=number,
            label="VERIFIED", witness_source="SIMULATOR_WITNESS", reason="synthetic",
        ))

    def capsule(outcome, *, scope=tenant, incident=incident_id):
        return dict(
            id=uuid4(), tenant_id=scope, incident_id=incident, outcome_revision_id=outcome,
            consent_epoch=1, tenant_consent_epoch=0, target_digest="0" * 64,
            evidence_ids=[], status="VALID", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

    def edges():
        return conn.execute(sa.select(
            lineage.c.tenant_id, lineage.c.incident_id, lineage.c.from_type,
            lineage.c.from_id, lineage.c.to_type, lineage.c.to_id, lineage.c.relation,
        ).where(lineage.c.incident_id.in_((incident_id, other_incident_id)))).all()

    with Operations.context(MigrationContext.configure(conn)):
        if installed:
            revision.downgrade()
            # A grant inherited from the migration user's defaults must fail closed.
            with pytest.raises(sa.exc.DBAPIError, match="must have owner-only access"):
                with conn.begin_nested():
                    conn.exec_driver_sql(
                        "ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO opsblaze_projector"
                    )
                    revision.upgrade()

        prior = capsule(old_outcome)
        conn.execute(capsules.insert().values(prior))
        revision.upgrade()
        assert edges() == []

        valid = capsule(valid_outcome)
        wrong_owner = capsule(owner_outcome, scope=foreign, incident=other_incident_id)
        # The individual FKs accept these rows. Only the scoped join may produce an edge.
        conn.execute(capsules.insert().values([
            valid, capsule(wrong_incident), capsule(wrong_tenant), wrong_owner,
        ]))
        expected = (tenant, incident_id, "experience", valid["id"], "outcome", valid_outcome, "DERIVED_FROM")
        assert edges() == [expected]

        # A retry against an identical projection must retain the existing row.
        conn.execute(capsules.delete().where(capsules.c.id == valid["id"]))
        conn.execute(lineage.update().where(lineage.c.from_id == valid["id"]).values(
            created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ))
        conn.execute(capsules.insert().values(valid))
        assert edges() == [expected]
        assert conn.scalar(sa.select(lineage.c.created_at).where(
            lineage.c.from_id == valid["id"],
        )).year == 2000

        pending = capsule(retry_outcome)
        with conn.begin_nested() as attempt:
            conn.execute(capsules.insert().values(pending))
            assert len(edges()) == 2
            attempt.rollback()
        assert conn.scalar(sa.select(capsules.c.id).where(capsules.c.id == pending["id"])) is None
        assert edges() == [expected]

        revision.downgrade()
        conn.execute(capsules.insert().values(pending))
        assert edges() == [expected]


def test_sqlite_experience_projection_and_downgrade():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            Base.metadata.create_all(conn)
            check_projection(conn, installed=False)
    finally:
        engine.dispose()


@pytest.mark.parametrize("method", ["upgrade", "downgrade"])
def test_experience_migration_rejects_unsupported_dialects(method):
    revision = migration("0046_experience_edge.py")
    with Operations.context(MigrationContext.configure(dialect_name="mysql")):
        with pytest.raises(NotImplementedError, match="does not support mysql"):
            getattr(revision, method)()


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_postgresql_experience_projection_and_downgrade():
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            # Roll back the DDL and fixture rows so subsequent tests retain the installed head.
            async with conn.begin() as transaction:
                await conn.run_sync(lambda sync_conn: check_projection(sync_conn, installed=True))
                await transaction.rollback()
    finally:
        await engine.dispose()
