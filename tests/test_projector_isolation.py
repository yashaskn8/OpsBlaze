"""E1: no Temporal client in the projector process or shared outbox claims."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import OutboxEntry
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.workers.experience_projector import (
    projector_database_url, run_projector_once,
)
from backend.workers.outbox import claim_intent


@pytest.mark.asyncio
async def test_redteam_projector_cannot_claim_temporal_signals_and_vice_versa(
    sessions_fixture,
):
    sessions = sessions_fixture
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "isolated",
            IncidentCreate(target_id="synthetic"),
        )
    # Before START_WORKFLOW is delivered the local projector cannot run.
    async with sessions() as session:
        async with session.begin():
            session.add(OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": str(uuid4())},
            ))
    assert await claim_intent(sessions, domain="experience") is None
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.delivered_at = datetime.now(timezone.utc)
            session.add(OutboxEntry(
                incident_id=incident.id, kind="CANCEL_WORKFLOW",
            ))

    # A future Temporal signal is not held hostage by memory projection.
    temporal = await claim_intent(sessions, domain="temporal")
    assert temporal is not None and temporal[3] == "CANCEL_WORKFLOW"
    # Projector still owns only its local intent.
    projected = await claim_intent(sessions, domain="experience")
    assert projected is not None and projected[3] == "PROJECT_EXPERIENCE"
    assert projected[0] != temporal[0]
    async with sessions() as session:
        async with session.begin():
            row = await session.get(OutboxEntry, projected[0])
            row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await run_projector_once(sessions)
    async with sessions() as session:
        entry = await session.get(OutboxEntry, projected[0])
        assert entry.delivered_at is not None  # Unverified source is safely skipped.


@pytest.mark.asyncio
async def test_redteam_projection_awaits_older_unacknowledged_workflow_signal(
    sessions_fixture,
):
    sessions = sessions_fixture
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "wait-start",
            IncidentCreate(target_id="synthetic"),
        )
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.delivered_at = datetime.now(timezone.utc)
            prior = OutboxEntry(
                incident_id=incident.id, kind="PLAN_DECIDED",
                available_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            session.add(prior)
            await session.flush()
            session.add(OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": str(uuid4())},
            ))
    assert await claim_intent(sessions, domain="experience") is None


def test_redteam_projector_credentials_must_be_separate_and_postgresql():
    primary = "postgresql+asyncpg://opsblaze:secret@localhost:5432/opsblaze"
    dedicated = (
        "postgresql+asyncpg://opsblaze_projector:distinct@localhost:5432/opsblaze"
    )
    assert projector_database_url(Settings(
        database_url=primary, projector_database_url=dedicated,
    )) == dedicated
    for bad in (
        "", primary,
        "postgresql+asyncpg://opsblaze:another@localhost:5432/opsblaze",
        "sqlite+aiosqlite:///test.db",
        "postgresql+asyncpg://postgres:secret@localhost:5432/opsblaze",
        "postgresql+asyncpg://other_service:secret@localhost:5432/opsblaze",
        "postgresql+asyncpg://opsblaze_projector:x@otherhost:5432/opsblaze",
    ):
        with pytest.raises(ValueError, match="projector"):
            projector_database_url(Settings(
                database_url=primary, projector_database_url=bad,
            ))
    with pytest.raises(ValueError, match="domain"):
        import asyncio
        asyncio.run(claim_intent(None, domain="unknown"))


def test_api_reference_has_no_secret_but_worker_requires_own_password():
    from backend.workers.experience_projector import (
        validate_projector_identity_reference,
    )

    public = "postgresql+asyncpg://opsblaze_projector@localhost:5432/opsblaze"
    primary = "postgresql+asyncpg://opsblaze@localhost:5432/opsblaze"
    settings = Settings(database_url=primary, projector_database_url=public)
    validate_projector_identity_reference(settings)
    with pytest.raises(ValueError, match="password"):
        projector_database_url(settings)

    for invalid in (
        "", primary,
        "postgresql+asyncpg://opsblaze_projector@remote:5432/opsblaze",
        "sqlite+aiosqlite:///test.db",
    ):
        with pytest.raises(ValueError, match="projector"):
            validate_projector_identity_reference(Settings(
                database_url=primary, projector_database_url=invalid,
            ))
