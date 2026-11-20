"""E1 expiry must scrub content and revoke dependent memory."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.experiences import capture_capsule, list_capsules, set_consent
from backend.memory.lessons import create_draft, retrieve_active_lessons
from backend.models import ExperienceCapsule, OperationalLesson, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.retention import expire_capsules


@pytest.mark.asyncio
async def test_expired_capsule_scrub_and_lesson_revocation(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "expiry", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="isolated fixture",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "tenant", incident.id)
    async with sessions_fixture() as session:
        lesson = await create_draft(
            session, "tenant", capsule.id, "CHECK_VERSION"
        )
    cutoff = datetime.now(timezone.utc)
    async with sessions_fixture() as session:
        async with session.begin():
            stored = await session.get(ExperienceCapsule, capsule.id)
            stored.expires_at = cutoff - timedelta(seconds=1)
    result = await expire_capsules(sessions_fixture, now=cutoff)
    assert result == {"capsules_scrubbed": 1, "lessons_revoked": 1}
    assert await expire_capsules(sessions_fixture, now=cutoff) == {
        "capsules_scrubbed": 0, "lessons_revoked": 0,
    }
    async with sessions_fixture() as session:
        expired = await session.get(ExperienceCapsule, capsule.id)
        revoked = await session.get(OperationalLesson, lesson.id)
        assert expired.status == "EXPIRED"
        assert expired.target_digest == "" and expired.evidence_ids == []
        assert revoked.status == "REVOKED" and revoked.target_digest == ""
        assert await list_capsules(session, "tenant") == []
        assert await retrieve_active_lessons(session, "tenant", "checkout") == []
