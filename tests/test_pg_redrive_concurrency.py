"""PostgreSQL one-winner redrive under parallel security-administrator requests.

This uses the disposable CI database only; no external adapter or target.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.experiences import set_consent
from backend.models import OutboxEntry, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings
from backend.workers.dead_letters import (
    DeadLetterConflict, redrive_projection_dead_letter,
)


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_two_administrators_cannot_create_duplicate_redrive_intents():
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "redrive-race-" + uuid4().hex[:12]
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "source",
                IncidentCreate(target_id="synthetic-only"),
            )
        async with sessions() as session:
            await set_consent(session, tenant, incident.id, 0, True)
        source_outcome = uuid4()
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=source_outcome, tenant_id=tenant,
                    incident_id=incident.id, revision=1,
                    label="VERIFIED", effect_id=None,
                    witness_source="SIMULATOR_WITNESS",
                    observed_version=2,
                    reason="synthetic verified fixture",
                ))
                source = OutboxEntry(
                    incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                    payload={"outcome_id": str(source_outcome)},
                    dead_lettered_at=datetime.now(timezone.utc),
                    dead_letter_code="RETRY_EXHAUSTED",
                    dead_letter_reviewed_at=datetime.now(timezone.utc),
                    dead_letter_reviewed_by="oidc:operator",
                    created_at=datetime.now(timezone.utc),
                    attempt=5,
                )
                session.add(source)
                await session.flush()
                source_id = source.id

        async def try_once(who: str) -> str:
            async with sessions() as session:
                try:
                    row = await redrive_projection_dead_letter(
                        session, tenant, source_id, 5, who,
                        "STORE_RESTORED",
                    )
                    assert row is not None
                    return "ACCEPTED"
                except (DeadLetterConflict, IntegrityError):
                    return "CONFLICT"

        results = await asyncio.gather(
            try_once("oidc:security-admin-a"),
            try_once("oidc:security-admin-b"),
        )
        assert sorted(results) == ["ACCEPTED", "CONFLICT"]

        async with sessions() as session:
            children = list((await session.scalars(
                select(OutboxEntry).where(
                    OutboxEntry.redrive_source_id == source_id
                )
            )).all())
            assert len(children) == 1
            assert children[0].redrive_authorized_by in {
                "oidc:security-admin-a", "oidc:security-admin-b",
            }
            assert children[0].payload == {"outcome_id": str(source_outcome)}
            original = await session.get(OutboxEntry, source_id)
            assert original.dead_lettered_at is not None
            assert original.delivered_at is None
            count = await session.scalar(
                select(func.count()).select_from(OutboxEntry)
                .where(OutboxEntry.redrive_source_id == source_id)
            )
            assert count == 1
    finally:
        await engine.dispose()
