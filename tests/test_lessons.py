
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import capture_capsule, set_consent
from backend.memory.lessons import (
    LessonConflict, create_draft, retrieve_active_lessons,
    revoke_lesson, validate_draft,
)
from backend.models import OperationalLesson, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def lesson_source(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "lesson", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await set_consent(session, "t", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS",
                observed_version=2, reason="independent witness",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "t", incident.id)
    return sessions_fixture, incident.id, capsule.id


@pytest.mark.asyncio
async def test_lesson_draft_validation_stays_out_of_live_retrieval(lesson_source):
    sessions, incident_id, capsule_id = lesson_source
    async with sessions() as session:
        lesson = await create_draft(session, "t", capsule_id, "CHECK_VERSION")
    async with sessions() as session:
        validated = await validate_draft(session, "t", lesson.id, 1)
        assert validated.status == "VALIDATED"
        assert validated.version == 2
    async with sessions() as session:
        assert await retrieve_active_lessons(session, "t", "checkout") == []
        assert await retrieve_active_lessons(session, "other", "checkout") == []


@pytest.mark.asyncio
async def test_consent_revocation_blocks_validation(lesson_source):
    sessions, incident_id, capsule_id = lesson_source
    async with sessions() as session:
        lesson = await create_draft(session, "t", capsule_id, "CHECK_VERSION")
    async with sessions() as session:
        await set_consent(session, "t", incident_id, 1, False)
    async with sessions() as session:
        with pytest.raises(LessonConflict):
            await validate_draft(session, "t", lesson.id, 1)


@pytest.mark.asyncio
async def test_outcome_correction_blocks_validation(lesson_source):
    sessions, incident_id, capsule_id = lesson_source
    async with sessions() as session:
        lesson = await create_draft(session, "t", capsule_id, "CHECK_VERSION")
    async with sessions() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident_id,
                revision=2, label="DISPUTED", effect_id=None,
                witness_source="OPERATOR_DISPUTE",
                observed_version=2, reason="later correction",
            ))
    async with sessions() as session:
        with pytest.raises(LessonConflict):
            await validate_draft(session, "t", lesson.id, 1)


@pytest.mark.asyncio
async def test_lesson_revocation_and_allowlist(lesson_source):
    sessions, incident_id, capsule_id = lesson_source
    async with sessions() as session:
        with pytest.raises(LessonConflict):
            await create_draft(session, "t", capsule_id, "GRANT_ADMIN")
    async with sessions() as session:
        lesson = await create_draft(
            session, "t", capsule_id, "ESCALATE_UNCERTAIN"
        )
    async with sessions() as session:
        revoked = await revoke_lesson(session, "t", lesson.id, 1)
        assert revoked.status == "REVOKED" and revoked.version == 2
    async with sessions() as session:
        with pytest.raises(LessonConflict):
            await validate_draft(session, "t", lesson.id, 1)
