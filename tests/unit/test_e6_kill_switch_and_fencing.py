"""Unit tests for Feature E6 — Emergency Kill-Switch and Consequential Mutation Fencing."""

from __future__ import annotations

import uuid

import pytest

from backend.domain.release import CanaryArm
from backend.persistence.models import Incident, Tenant
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.release_service import PolicyReleaseService
from backend.releases.router_service import PolicyRouterService


@pytest.fixture
async def active_canary_setup(session_factory):
    tenant_id = uuid.uuid4()
    bundle_svc = PolicyBundleService(session_factory)
    release_svc = PolicyReleaseService(session_factory)

    async with session_factory() as session:
        t = Tenant(id=tenant_id, name=f"test-tenant-fencing-{tenant_id}")
        session.add(t)
        await session.commit()

    incumbent = await bundle_svc.ensure_bootstrap_incumbent(tenant_id=tenant_id, target_component="investigator")
    candidate = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="candidate-fencing",
        base_policy_version="v2.0.0",
        target_component="investigator",
        created_by="drafter",
        model_id="gpt-4o",
        model_version="2026-11",
        tool_catalog_version="v2",
        a2_grant_version="v2",
        source_provenance_digest="4" * 64,
    )
    sealed_candidate = await bundle_svc.seal_bundle(tenant_id=tenant_id, bundle_id=candidate.id)

    rel = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=sealed_candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter",
    )
    rel = await release_svc.submit_for_approval(tenant_id=tenant_id, release_id=rel.id, expected_version=1, actor_id="drafter", actor_role="drafter")
    rel = await release_svc.review_release(tenant_id=tenant_id, release_id=rel.id, expected_version=2, reviewer_id="reviewer", reviewer_role="release_owner", decision="APPROVED")
    rel = await release_svc.activate_canary(tenant_id=tenant_id, release_id=rel.id, expected_version=3, canary_percentage=50, actor_id="reviewer", actor_role="release_owner")

    return tenant_id, rel, incumbent, sealed_candidate


@pytest.mark.asyncio
async def test_kill_switch_instantly_isolates_and_fences(
    session_factory, active_canary_setup
) -> None:
    """When kill switch is tripped, new traffic routes to incumbent and active incident mutations are fenced."""
    tenant_id, release, incumbent, candidate = active_canary_setup
    release_svc = PolicyReleaseService(session_factory)
    router_svc = PolicyRouterService(session_factory, release_svc)

    # 1. Admit an incident onto the CANDIDATE arm while canary is healthy
    cand_incident_id = uuid.uuid4()
    async with session_factory() as session:
        cand_inc = Incident(
            id=cand_incident_id,
            tenant_id=tenant_id,
            client_key="cand-inc-01",
            request_sha256="req1",
            target_id="target-01",
            status_projection="INVESTIGATING",
            status_version=1,
            pinned_policy_bundle_id=candidate.id,
            policy_release_id=release.id,
            canary_arm=CanaryArm.CANDIDATE.value,
        )
        session.add(cand_inc)
        await session.commit()

    # Before kill switch: consequential actions allowed
    allowed, err = await router_svc.verify_consequential_mutation_allowed(
        tenant_id=tenant_id, incident_id=cand_incident_id
    )
    assert allowed is True
    assert err is None

    # 2. Trip Emergency Kill Switch
    await release_svc.trip_kill_switch(
        tenant_id=tenant_id,
        release_id=release.id,
        expected_version=release.version,
        justification="Critical memory leak detected in candidate policy",
        actor_id="sre-lead-dave",
        actor_role="sre_lead",
    )

    # 3. After kill switch: consequential actions on that incident are now strictly FENCED
    allowed_after, err_after = await router_svc.verify_consequential_mutation_allowed(
        tenant_id=tenant_id, incident_id=cand_incident_id
    )
    assert allowed_after is False
    assert "Kill switch active" in (err_after or "")

    # 4. New admissions are now routed 100% to INCUMBENT
    arm, b_id, _rel_id, _ = await router_svc.route_incident(
        tenant_id=tenant_id,
        incident_id=uuid.uuid4(),
        target_id="target-01",
        target_component="investigator",
    )
    assert arm == CanaryArm.INCUMBENT
    assert b_id == incumbent.id
