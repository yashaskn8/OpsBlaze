
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import ConsentConflict, capture_capsule, list_capsules, set_consent
from backend.models import ExperienceCapsule, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_capture_requires_opt_in_and_verified_outcome(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t", "k", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        with pytest.raises(ConsentConflict):
            await capture_capsule(session, "t", incident.id)
    async with sessions_fixture() as session:
        consent = await set_consent(session, "t", incident.id, 0, True)
        assert consent.allowed and consent.epoch == 1
    async with sessions_fixture() as session:
        with pytest.raises(ConsentConflict):
            await capture_capsule(session, "t", incident.id)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS",
                observed_version=2, reason="fixture verification",
            ))
    async with sessions_fixture() as session:
        first = await capture_capsule(session, "t", incident.id)
        assert first.status == "VALID"
    async with sessions_fixture() as session:
        second = await capture_capsule(session, "t", incident.id)
        assert first.id == second.id
        visible = await list_capsules(session, "t")
        assert [capsule.id for capsule in visible] == [first.id]
        assert await list_capsules(session, "other") == []


@pytest.mark.asyncio
async def test_revocation_tombstones_captured_metadata(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t", "k", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        await set_consent(session, "t", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS",
                observed_version=2, reason="fixture verification",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "t", incident.id)
    async with sessions_fixture() as session:
        with pytest.raises(ConsentConflict):
            await set_consent(session, "t", incident.id, 0, False)
    async with sessions_fixture() as session:
        revoked = await set_consent(session, "t", incident.id, 1, False)
        assert revoked.epoch == 2
    async with sessions_fixture() as session:
        assert await list_capsules(session, "t") == []
        stored = await session.get(ExperienceCapsule, capsule.id)
        assert stored.status == "REVOKED" and stored.evidence_ids == []
        assert stored.target_digest == ""


@pytest.mark.asyncio
async def test_read_time_excludes_superseded_verified_source(sessions_fixture):
    """Even out-of-band outcome revision cannot expose an obsolete capsule."""
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "superceded", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS", observed_version=3,
                reason="initial verification",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "tenant", incident.id)
        assert len(await list_capsules(session, "tenant")) == 1
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=2, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE", observed_version=3,
                reason="independent reversal",
            ))
    async with sessions_fixture() as session:
        assert await list_capsules(session, "tenant") == []
        persisted = await session.get(ExperienceCapsule, capsule.id)
        assert persisted.status == "VALID"
