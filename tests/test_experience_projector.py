"""E1 automatic experience projection: exact source, consent and crash replay."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import list_capsules, set_consent
from backend.models import ExperienceCapsule, OutboxEntry, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.experience_projector import (
    project_experience_outbox, run_projector_once,
)


async def setup_projector_lab(sessions, *, allowed: bool, superseded: bool = False):
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", str(uuid4()), IncidentCreate(target_id="checkout"),
        )
    if allowed:
        async with sessions() as session:
            await set_consent(session, "tenant", incident.id, 0, True)
    outcome_id = uuid4()
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.incident_id == incident.id,
                    OutboxEntry.kind == "START_WORKFLOW",
                )
            )
            start.delivered_at = datetime.now(timezone.utc)
            session.add(OutcomeRevision(
                id=outcome_id, tenant_id="tenant", incident_id=incident.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS", observed_version=1,
                reason="synthetic independent verification",
            ))
            session.add(OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": str(outcome_id)},
            ))
            if superseded:
                session.add(OutcomeRevision(
                    id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                    revision=2, label="DISPUTED", effect_id=None,
                    witness_source="OPERATOR_DISPUTE", observed_version=1,
                    reason="synthetic correction",
                ))
    return incident.id, outcome_id


@pytest.mark.asyncio
async def test_projector_is_idempotent_after_lost_ack(sessions_fixture):
    sessions = sessions_fixture
    incident_id, outcome_id = await setup_projector_lab(sessions, allowed=True)

    assert await run_projector_once(sessions)
    async with sessions() as session:
        capsules = await list_capsules(session, "tenant")
        assert len(capsules) == 1
        assert capsules[0].outcome_revision_id == outcome_id
        projection = await session.scalar(select(OutboxEntry).where(
            OutboxEntry.incident_id == incident_id,
            OutboxEntry.kind == "PROJECT_EXPERIENCE",
        ))
        assert projection.delivered_at is not None

    # Crash after capsule commit but before outbox ACK: reprocessing must
    # return the exact existing source and never duplicate memory.
    async with sessions() as session:
        async with session.begin():
            projection = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident_id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
            ))
            projection.delivered_at = None
            projection.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await run_projector_once(sessions)
    async with sessions() as session:
        assert len(await list_capsules(session, "tenant")) == 1
        assert len((await session.scalars(
            select(ExperienceCapsule).where(
                ExperienceCapsule.incident_id == incident_id,
            )
        )).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,superseded", [
    (False, False), (True, True),
])
async def test_projector_skips_ineligible_sources_without_blocking_dispatch(
    sessions_fixture, allowed, superseded,
):
    sessions = sessions_fixture
    incident_id, _ = await setup_projector_lab(
        sessions, allowed=allowed, superseded=superseded,
    )
    assert await run_projector_once(sessions)
    async with sessions() as session:
        assert await list_capsules(session, "tenant") == []
        projection = await session.scalar(select(OutboxEntry).where(
            OutboxEntry.incident_id == incident_id,
            OutboxEntry.kind == "PROJECT_EXPERIENCE",
        ))
        assert projection.delivered_at is not None


@pytest.mark.asyncio
async def test_projector_retry_preserves_intent_on_store_outage(sessions_fixture):
    sessions = sessions_fixture
    incident_id, _ = await setup_projector_lab(sessions, allowed=True)
    with patch(
        "backend.workers.experience_projector.project_experience_outbox",
        side_effect=ConnectionError("synthetic DB outage"),
    ):
        assert await run_projector_once(sessions)

    async with sessions() as session:
        row = await session.scalar(select(OutboxEntry).where(
            OutboxEntry.incident_id == incident_id,
            OutboxEntry.kind == "PROJECT_EXPERIENCE",
        ))
        assert row.attempt == 1 and row.delivered_at is None
        assert await list_capsules(session, "tenant") == []

    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident_id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
            ))
            row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await run_projector_once(sessions)
    async with sessions() as session:
        assert len(await list_capsules(session, "tenant")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    None, {}, {"outcome_id": "not-a-uuid"},
    {"outcome_id": str(uuid4()), "allow": True},
    {"outcome_id": 123},
])
async def test_redteam_projector_never_accepts_untyped_source_payload(
    sessions_fixture, payload,
):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", str(uuid4()), IncidentCreate(target_id="checkout"),
        )
    with pytest.raises(ValueError):
        await project_experience_outbox(
            sessions_fixture, "tenant", str(incident.id), payload,
        )
