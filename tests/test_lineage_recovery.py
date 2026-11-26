"""Exercise the runtime sweep and its atomic PostgreSQL checkpoint."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.models import Base, Evidence, HypothesisStep, Incident, LineageEdge, LineageRecoveryCheckpoint
from backend.settings import get_settings
from backend.workers import dispatcher
from backend.workers import lineage_recovery as worker


async def seed_incident(sessions, tenant, *, incident_id=None):
    incident_id = incident_id or uuid4()
    evidence_id = uuid4()
    sources = sorted([uuid4(), uuid4()])
    async with sessions.begin() as session:
        session.add(Incident(
            id=incident_id, tenant_id=tenant, target_id="synthetic", client_key=str(incident_id),
            request_sha256="0" * 64,
        ))
        await session.flush()
        session.add(Evidence(
            id=evidence_id, tenant_id=tenant, incident_id=incident_id, target_id="synthetic",
            observed_at=datetime.now(timezone.utc), source="simulator.health", source_version="simulator-v1",
            sha256="0" * 64, redacted_excerpt="{}", classification="INTERNAL", validity="VALID",
        ))
        session.add_all([
            HypothesisStep(
                id=source, incident_id=incident_id, step_no=index, model_id="baseline",
                prompt_version="none", evidence_ids=[str(evidence_id)], cause_scores={},
                probe_id="simulator.health", decision="ABSTAINED",
            )
            for index, source in enumerate(sources, start=1)
        ])
    return incident_id, evidence_id, sources


def edge(tenant, incident, source, evidence):
    return LineageEdge(
        tenant_id=tenant, incident_id=incident, from_type="hypothesis", from_id=source,
        to_type="evidence", to_id=evidence, relation="CITES",
    )


async def projected_ids(session, incident):
    return set((await session.scalars(select(LineageEdge.from_id).where(
        LineageEdge.incident_id == incident,
    ))).all())


async def test_runtime_sweep_rebuilds_and_reaps_in_pages_across_tenants(sessions_fixture):
    own, evidence, sources = await seed_incident(sessions_fixture, "own", incident_id=UUID(int=1))
    other, foreign, other_sources = await seed_incident(sessions_fixture, "other", incident_id=UUID(int=2))
    orphan = UUID(int=3)
    async with sessions_fixture.begin() as session:
        session.add(LineageRecoveryCheckpoint(id=1))
        retained = edge("own", own, sources[0], evidence)
        retained.created_at = datetime(2001, 1, 1, tzinfo=timezone.utc)
        session.add_all([retained, edge("own", own, orphan, evidence)])

    now = datetime.now(timezone.utc)
    assert await worker.recover_lineage_page(sessions_fixture, limit=1, now=now)
    async with sessions_fixture() as session:
        checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
        assert checkpoint.incident_id == own and checkpoint.source_cursor == sources[0]
        assert await projected_ids(session, own) == {sources[0], orphan}
        assert await projected_ids(session, other) == set()

    # A fresh session per iteration models restart with committed cursor state.
    orphan_cursor_seen = False
    for _ in range(30):
        if not await worker.recover_lineage_page(sessions_fixture, limit=1, now=now):
            break
        async with sessions_fixture() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            if checkpoint.incident_id == own and checkpoint.edge_cursor is not None:
                orphan_cursor_seen = True
                assert await projected_ids(session, other) == set()
    else:
        pytest.fail("bounded two-incident sweep did not complete")
    assert orphan_cursor_seen

    async with sessions_fixture() as session:
        checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
        assert checkpoint.completed_at is not None and checkpoint.incident_id is None
        assert await projected_ids(session, own) == set(sources)
        assert await projected_ids(session, other) == set(other_sources)
        retained = await session.scalar(select(LineageEdge).where(
            LineageEdge.incident_id == own, LineageEdge.from_id == sources[0],
        ))
        assert retained.created_at.year == 2001
        assert await session.scalar(select(LineageEdge.to_id).where(
            LineageEdge.incident_id == other, LineageEdge.from_id == other_sources[0],
        )) == foreign
    assert not await worker.recover_lineage_page(sessions_fixture, limit=1, now=now)
    assert await worker.recover_lineage_page(
        sessions_fixture, limit=1, now=now + timedelta(minutes=1),
    )


async def test_dispatcher_starts_recovery_against_its_real_store(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'dispatcher.db').as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        incident, _, sources = await seed_incident(sessions, "runtime")
        async with sessions.begin() as session:
            session.add(LineageRecoveryCheckpoint(id=1))
        monkeypatch.setattr(dispatcher, "get_session_maker", lambda: sessions)
        monkeypatch.setattr(dispatcher, "connect_temporal", AsyncMock())
        monkeypatch.setattr(dispatcher, "heartbeat_loop", AsyncMock())
        service = asyncio.create_task(dispatcher.main())
        try:
            async with asyncio.timeout(5):
                while True:
                    async with sessions() as session:
                        if await projected_ids(session, incident) == set(sources):
                            break
                    await asyncio.sleep(0.025)
            assert not service.done()
        finally:
            service.cancel()
            with suppress(asyncio.CancelledError):
                await service
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_native_recovery_checkpoints_only_committed_pages():
    engine = create_async_engine(get_settings().database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"recovery-{uuid4().hex}"
    incident, evidence, sources = await seed_incident(sessions, tenant)
    try:
        async with sessions.begin() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1, with_for_update=True)
            assert checkpoint is not None  # Seeded by migration 0048.
            checkpoint.incident_id = incident
            checkpoint.phase = "hypothesis"
            checkpoint.source_cursor = checkpoint.edge_cursor = None
            assert not await asyncio.wait_for(worker.recover_lineage_page(sessions), 5)

        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "ALTER TABLE public.lineage_edge ADD CONSTRAINT test_lineage_recovery_failure "
                f"CHECK (from_id <> '{sources[1]}'::uuid) NOT VALID"
            )
        try:
            assert await worker.recover_lineage_page(sessions, limit=1)
            with pytest.raises(IntegrityError, match="test_lineage_recovery_failure"):
                await worker.recover_lineage_page(sessions, limit=1)
            async with sessions() as session:
                checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
                assert checkpoint.phase == "hypothesis" and checkpoint.source_cursor == sources[0]
                assert await projected_ids(session, incident) == {sources[0]}
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "ALTER TABLE public.lineage_edge DROP CONSTRAINT test_lineage_recovery_failure"
                ))
        assert await worker.recover_lineage_page(sessions, limit=1)
        async with sessions() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            assert checkpoint.phase == "plan" and checkpoint.source_cursor is None
            assert await projected_ids(session, incident) == set(sources)

        async with sessions.begin() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            checkpoint.phase = "orphans"
            session.add(edge(tenant, incident, UUID(int=0), evidence))
        assert await worker.recover_lineage_page(sessions, limit=1)
        async with sessions() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            assert checkpoint.edge_cursor[1] == str(UUID(int=0))
            assert await projected_ids(session, incident) == set(sources)
        assert await worker.recover_lineage_page(sessions, limit=1)
        assert await worker.recover_lineage_page(sessions, limit=1)
        async with sessions() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            assert checkpoint.phase is None and checkpoint.edge_cursor is None
            assert await projected_ids(session, incident) == set(sources)
    finally:
        async with sessions.begin() as session:
            checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
            checkpoint.incident_id = checkpoint.phase = checkpoint.source_cursor = checkpoint.edge_cursor = None
        await engine.dispose()
