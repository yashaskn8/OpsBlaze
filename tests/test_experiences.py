
import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.experiences import (
    ConsentConflict, capture_capsule, export_privacy_page, list_capsules,
    set_consent,
)
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


@pytest.mark.asyncio
async def test_redteam_capsule_read_rejects_forged_source_target(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "poisoned-capsule",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS",
                observed_version=1, reason="witness fixture",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "tenant", incident.id)
    async with sessions_fixture() as session:
        assert [x.id for x in await list_capsules(session, "tenant")] == [capsule.id]
    async with sessions_fixture() as session:
        async with session.begin():
            row = await session.get(ExperienceCapsule, capsule.id)
            row.target_digest = hashlib.sha256(b"tenant:other").hexdigest()
    async with sessions_fixture() as session:
        assert await list_capsules(session, "tenant") == []
    async with sessions_fixture() as session:
        with pytest.raises(ConsentConflict, match="stale"):
            await capture_capsule(session, "tenant", incident.id)


@pytest.mark.asyncio
async def test_redteam_stale_consent_epoch_cannot_reuse_old_capsule(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "stale-capsule",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS",
                observed_version=1, reason="witness fixture",
            ))
    async with sessions_fixture() as session:
        old = await capture_capsule(session, "tenant", incident.id)
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 1, True)
    async with sessions_fixture() as session:
        assert await list_capsules(session, "tenant") == []
    async with sessions_fixture() as session:
        with pytest.raises(ConsentConflict, match="stale"):
            await capture_capsule(session, "tenant", incident.id)
    async with sessions_fixture() as session:
        stored = await session.get(ExperienceCapsule, old.id)
        assert stored.status == "VALID" and stored.consent_epoch == 1


@pytest.mark.asyncio
async def test_redteam_privacy_export_is_bounded_redacted_and_tenant_scoped(
    sessions_fixture,
):
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "t", "privacy-a", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "t", "privacy-b", IncidentCreate(target_id="payments"),
        )
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "other", "privacy-foreign",
            IncidentCreate(target_id="private-other-service"),
        )
    for tenant, incident, allowed in (
        ("t", first, True), ("t", second, False), ("other", foreign, True),
    ):
        async with sessions_fixture() as session:
            await set_consent(session, tenant, incident.id, 0, allowed)

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=first.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="privacy fixture only",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "t", first.id)
    async with sessions_fixture() as session:
        await set_consent(session, "t", first.id, 1, False)

    async with sessions_fixture() as session:
        start = await export_privacy_page(session, "t", "consents", limit=1)
        assert len(start["records"]) == 1 and start["next_cursor"]
        more = await export_privacy_page(
            session, "t", "consents", cursor=UUID(start["next_cursor"]),
            limit=1,
        )
        assert len(more["records"]) == 1 and more["next_cursor"] is None
        assert {x["incident_id"] for x in (
            start["records"] + more["records"]
        )} == {str(first.id), str(second.id)}
        assert await export_privacy_page(
            session, "other", "consents",
        ) != start
        own_capsules = await export_privacy_page(session, "t", "capsules")
        assert len(own_capsules["records"]) == 1
        entry = own_capsules["records"][0]
        assert entry["id"] == str(capsule.id)
        assert entry["incident_id"] == str(first.id)
        assert entry["outcome_revision_id"] == str(capsule.outcome_revision_id)
        assert entry["consent_epoch"] == 1 and entry["status"] == "REVOKED"
        assert entry["created_at"] and entry["expires_at"]
        assert own_capsules["metadata_only"] is True
        assert all(key not in str(own_capsules) for key in (
            "target_digest", "evidence_ids", "redacted_excerpt", "private-other-service",
        ))
        assert (await export_privacy_page(
            session, "other", "capsules",
        ))["records"] == []
        for invalid in (0, 51):
            with pytest.raises(ValueError):
                await export_privacy_page(session, "t", "consents", limit=invalid)
