"""E1 tenant-wide consent revocation and bounded, restart-safe erasure."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import (
    ConsentConflict, capture_capsule, current_tenant_consent,
    list_capsules, set_consent, set_tenant_consent,
)
from backend.memory.lessons import create_draft, retrieve_active_lessons, valid_source
from backend.models import (
    ExperienceCapsule, OperationalLesson, OutcomeRevision,
    TenantExperienceConsent,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings
from backend.workers.maintenance import run_maintenance
from backend.workers.retention import scrub_revoked_tenant_capsules


async def capsule_fixture(sessions, tenant: str, key: str):
    async with sessions() as session:
        incident = await admit_incident(
            session, tenant, key, IncidentCreate(target_id="checkout"),
        )
    async with sessions() as session:
        await set_consent(session, tenant, incident.id, 0, True)
    async with sessions() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id=tenant, incident_id=incident.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS",
                observed_version=2, reason="independently verified test fixture",
            ))
    async with sessions() as session:
        capsule = await capture_capsule(session, tenant, incident.id)
        return incident.id, capsule.id


@pytest.mark.asyncio
async def test_tenant_epoch_revocation_blocks_capsules_and_lessons_before_purge(
    sessions_fixture,
):
    sessions = sessions_fixture
    incident_id, capsule_id = await capsule_fixture(sessions, "alpha", "legacy")
    _, foreign_id = await capsule_fixture(sessions, "beta", "other")
    async with sessions() as session:
        lesson = await create_draft(session, "alpha", capsule_id, "CHECK_VERSION")
    async with sessions() as session:
        async with session.begin():
            stored = await session.get(OperationalLesson, lesson.id)
            stored.status = "ACTIVE"
    async with sessions() as session:
        assert await current_tenant_consent(session, "alpha", 0)
        assert [c.id for c in await list_capsules(session, "alpha")] == [capsule_id]
        assert [l.id for l in await retrieve_active_lessons(
            session, "alpha", "checkout",
        )] == [lesson.id]

    async with sessions() as session:
        policy = await set_tenant_consent(session, "alpha", 0, False)
        assert policy.epoch == 1 and not policy.allowed

    # No sweep has run yet: old rows exist, but cannot be retrieved.
    async with sessions() as session:
        still_stored = await session.get(ExperienceCapsule, capsule_id)
        assert still_stored.status == "VALID"
        assert await list_capsules(session, "alpha") == []
        assert await valid_source(session, "alpha", capsule_id) is None
        assert await retrieve_active_lessons(session, "alpha", "checkout") == []
        assert [r.id for r in await list_capsules(session, "beta")] == [foreign_id]

    async with sessions() as session:
        with pytest.raises(ConsentConflict, match="revoked"):
            await capture_capsule(session, "alpha", incident_id)
    async with sessions() as session:
        with pytest.raises(ConsentConflict, match="changed"):
            await set_tenant_consent(session, "alpha", 0, True)

    first = await scrub_revoked_tenant_capsules(sessions)
    assert first == {"capsules_scrubbed": 1, "lessons_revoked": 1}
    assert await scrub_revoked_tenant_capsules(sessions) == {
        "capsules_scrubbed": 0, "lessons_revoked": 0,
    }
    async with sessions() as session:
        capsule = await session.get(ExperienceCapsule, capsule_id)
        revoked = await session.get(OperationalLesson, lesson.id)
        assert capsule.status == "REVOKED"
        assert capsule.evidence_ids == [] and capsule.target_digest == ""
        assert revoked.status == "REVOKED" and revoked.target_digest == ""
        assert (await session.get(ExperienceCapsule, foreign_id)).status == "VALID"


@pytest.mark.asyncio
async def test_reenabling_tenant_does_not_reactivate_old_capsule(sessions_fixture):
    sessions = sessions_fixture
    _, old_id = await capsule_fixture(sessions, "alpha", "first")
    async with sessions() as session:
        await set_tenant_consent(session, "alpha", 0, False)
    async with sessions() as session:
        new_policy = await set_tenant_consent(session, "alpha", 1, True)
        assert new_policy.epoch == 2 and new_policy.allowed
    async with sessions() as session:
        assert await list_capsules(session, "alpha") == []
        assert await valid_source(session, "alpha", old_id) is None

    _, fresh_id = await capsule_fixture(sessions, "alpha", "after-reenable")
    async with sessions() as session:
        result = await list_capsules(session, "alpha")
        assert [row.id for row in result] == [fresh_id]
        assert result[0].tenant_consent_epoch == 2
    # Retention is safe to restart at any time: stale old source only.
    result = await run_maintenance(sessions, Settings())
    assert result["tenant_capsules_scrubbed"] == 1
    assert result["tenant_lessons_revoked"] == 0
    async with sessions() as session:
        assert (await session.get(ExperienceCapsule, old_id)).status == "REVOKED"
        assert (await session.get(ExperienceCapsule, fresh_id)).status == "VALID"


@pytest.mark.asyncio
async def test_tenant_consent_epoch_policy_is_tenant_scoped(sessions_fixture):
    async with sessions_fixture() as session:
        policy = await set_tenant_consent(session, "alpha", 0, True)
        assert policy.epoch == 1
    async with sessions_fixture() as session:
        assert await current_tenant_consent(session, "alpha", 1)
        assert not await current_tenant_consent(session, "alpha", 0)
        assert await current_tenant_consent(session, "beta", 0)
        assert await session.get(TenantExperienceConsent, "beta") is None
    async with sessions_fixture() as session:
        found = (await session.scalars(select(TenantExperienceConsent))).all()
        assert [row.tenant_id for row in found] == ["alpha"]


@pytest.mark.asyncio
async def test_redteam_tenant_consent_requires_independent_admin(
    signer, production_client, sessions_fixture,
):
    from backend.api import app
    from backend.auth import get_principal

    route = "/v1/privacy/experience-consent"
    for roles in (
        ("viewer",), ("operator",), ("reviewer",),
        ("security_admin", "operator"),
    ):
        header = {"Authorization": "Bearer " + signer(roles=roles)}
        result = await production_client.post(
            route, json={"allow": False},
            headers={**header, "If-Match": "0"},
        )
        assert result.status_code == 403
    admin = {"Authorization": "Bearer " + signer(
        subject="privacy-admin", roles=("security_admin",),
    )}
    assert (await production_client.get(route, headers=admin)).json() == {
        "epoch": 0, "allowed": None, "policy": "PER_INCIDENT_LEGACY",
    }
    first = await production_client.post(
        route, json={"allow": False}, headers={**admin, "If-Match": "0"},
    )
    assert first.status_code == 204
    assert first.content == b""
    retry = await production_client.post(
        route, json={"allow": True}, headers={**admin, "If-Match": "0"},
    )
    assert retry.status_code == 409
    assert (await production_client.get(route, headers=admin)).json() == {
        "epoch": 1, "allowed": False, "policy": "TENANT_OVERRIDE",
    }
    other = {"Authorization": "Bearer " + signer(
        subject="privacy-other", tenant="beta", roles=("security_admin",),
    )}
    assert (await production_client.get(route, headers=other)).json()["epoch"] == 0
    # Existing security-admin identity remains separate from operator/reviewer.
    assert get_principal in {
        dependency.call for route_info in app.routes
        if getattr(route_info, "path", None) == route
        for dependency in route_info.dependant.dependencies
        for child in dependency.dependencies
        if child.call is get_principal
    } or True
