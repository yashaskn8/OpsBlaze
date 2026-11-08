"""Unit tests for Feature E6 — Policy Release State Machine, CAS Concurrency & Separation of Duties."""

from __future__ import annotations

import uuid
import pytest

from backend.domain.release import (
    ActiveReleaseConflictError,
    ReleaseSeparationOfDutiesError,
    ReleaseState,
    StaleVersionConflictError,
)
from backend.persistence.models import Tenant
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.release_service import PolicyReleaseService


@pytest.fixture
async def setup_tenant_and_bundles(session_factory):
    tenant_id = uuid.uuid4()
    bundle_svc = PolicyBundleService(session_factory)

    async with session_factory() as session:
        t = Tenant(id=tenant_id, name=f"test-tenant-rel-{tenant_id}")
        session.add(t)
        await session.commit()

    incumbent = await bundle_svc.ensure_bootstrap_incumbent(
        tenant_id=tenant_id,
        target_component="investigator",
    )

    candidate = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="candidate-v2.0.0",
        base_policy_version="v2.0.0",
        target_component="investigator",
        created_by="drafter-alice",
        model_id="gpt-4o",
        model_version="2026-11",
        tool_catalog_version="v2",
        a2_grant_version="v2",
        source_provenance_digest="2" * 64,
    )
    sealed_candidate = await bundle_svc.seal_bundle(tenant_id=tenant_id, bundle_id=candidate.id)

    return tenant_id, incumbent, sealed_candidate


@pytest.mark.asyncio
async def test_full_release_lifecycle_happy_path(session_factory, setup_tenant_and_bundles) -> None:
    """Tests DRAFT -> PENDING_APPROVAL -> APPROVED -> SHADOW -> CANARY -> ACTIVE -> ROLLED_BACK."""
    tenant_id, incumbent, candidate = setup_tenant_and_bundles
    release_svc = PolicyReleaseService(session_factory)

    # 1. Create Draft Release
    rel = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter-alice",
    )
    assert rel.state == ReleaseState.DRAFT.value
    assert rel.version == 1

    # 2. Submit for Approval (v1 -> v2)
    rel = await release_svc.submit_for_approval(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=1,
        actor_id="drafter-alice",
        actor_role="drafter",
    )
    assert rel.state == ReleaseState.PENDING_APPROVAL.value
    assert rel.version == 2

    # 3. Review & Approve by distinct release_owner (v2 -> v3)
    rel = await release_svc.review_release(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=2,
        reviewer_id="reviewer-bob",
        reviewer_role="release_owner",
        decision="APPROVED",
        decision_comments="Candidate passed all E5 statistical regression gates.",
    )
    assert rel.state == ReleaseState.APPROVED.value
    assert rel.reviewer_id == "reviewer-bob"
    assert rel.version == 3

    # 4. Activate Shadow (v3 -> v4)
    rel = await release_svc.activate_shadow(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=3,
        actor_id="reviewer-bob",
        actor_role="release_owner",
    )
    assert rel.state == ReleaseState.SHADOW.value
    assert rel.canary_percentage == 0
    assert rel.version == 4

    # 5. Activate Canary at 20% (v4 -> v5)
    rel = await release_svc.activate_canary(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=4,
        canary_percentage=20,
        actor_id="reviewer-bob",
        actor_role="release_owner",
    )
    assert rel.state == ReleaseState.CANARY.value
    assert rel.canary_percentage == 20
    assert rel.version == 5

    # 6. Update Canary to 50% (v5 -> v6)
    rel = await release_svc.update_canary_percentage(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=5,
        new_percentage=50,
        actor_id="reviewer-bob",
        actor_role="release_owner",
    )
    assert rel.canary_percentage == 50
    assert rel.version == 6

    # 7. Promote to Active 100% (v6 -> v7)
    rel = await release_svc.promote_active(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=6,
        actor_id="reviewer-bob",
        actor_role="release_owner",
    )
    assert rel.state == ReleaseState.ACTIVE.value
    assert rel.canary_percentage == 100
    assert rel.version == 7

    # 8. Trigger Rollback (v7 -> v8)
    rel = await release_svc.trigger_rollback(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=7,
        rollback_reason="Manual test rollback after promotion",
        actor_id="operator-carol",
        actor_role="operator",
    )
    assert rel.state == ReleaseState.ROLLED_BACK.value
    assert rel.canary_percentage == 0
    assert rel.version == 8
    assert rel.rollback_reason == "Manual test rollback after promotion"

    # Verify audit events were recorded
    audits = await release_svc.list_release_audits(tenant_id=tenant_id, release_id=rel.id)
    assert len(audits) >= 7


@pytest.mark.asyncio
async def test_separation_of_duties_enforced(session_factory, setup_tenant_and_bundles) -> None:
    """Reviewer cannot be the same principal who drafted the release."""
    tenant_id, incumbent, candidate = setup_tenant_and_bundles
    release_svc = PolicyReleaseService(session_factory)

    rel = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter-alice",
    )
    await release_svc.submit_for_approval(
        tenant_id=tenant_id,
        release_id=rel.id,
        expected_version=1,
        actor_id="drafter-alice",
        actor_role="drafter",
    )

    # Alice attempts to self-approve -> must raise ReleaseSeparationOfDutiesError
    with pytest.raises(ReleaseSeparationOfDutiesError):
        await release_svc.review_release(
            tenant_id=tenant_id,
            release_id=rel.id,
            expected_version=2,
            reviewer_id="drafter-alice",
            reviewer_role="release_owner",
            decision="APPROVED",
        )


@pytest.mark.asyncio
async def test_cas_version_conflict_rejected(session_factory, setup_tenant_and_bundles) -> None:
    """Supplying a stale expected_version must fail with StaleVersionConflictError."""
    tenant_id, incumbent, candidate = setup_tenant_and_bundles
    release_svc = PolicyReleaseService(session_factory)

    rel = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter-alice",
    )

    # Expected version is 1, but caller supplies 999
    with pytest.raises(StaleVersionConflictError):
        await release_svc.submit_for_approval(
            tenant_id=tenant_id,
            release_id=rel.id,
            expected_version=999,
            actor_id="drafter-alice",
            actor_role="drafter",
        )


@pytest.mark.asyncio
async def test_anti_split_brain_single_active_release(session_factory, setup_tenant_and_bundles) -> None:
    """Cannot have two concurrent releases in SHADOW/CANARY/ACTIVE for the same component."""
    tenant_id, incumbent, candidate = setup_tenant_and_bundles
    release_svc = PolicyReleaseService(session_factory)

    # Release 1
    rel1 = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter-alice",
    )
    await release_svc.submit_for_approval(tenant_id=tenant_id, release_id=rel1.id, expected_version=1, actor_id="alice", actor_role="drafter")
    await release_svc.review_release(tenant_id=tenant_id, release_id=rel1.id, expected_version=2, reviewer_id="bob", reviewer_role="release_owner", decision="APPROVED")
    await release_svc.activate_canary(tenant_id=tenant_id, release_id=rel1.id, expected_version=3, canary_percentage=10, actor_id="bob", actor_role="release_owner")

    # Release 2
    rel2 = await release_svc.create_release(
        tenant_id=tenant_id,
        target_component="investigator",
        bundle_id=candidate.id,
        incumbent_bundle_id=incumbent.id,
        drafter_id="drafter-charlie",
    )
    await release_svc.submit_for_approval(tenant_id=tenant_id, release_id=rel2.id, expected_version=1, actor_id="charlie", actor_role="drafter")
    await release_svc.review_release(tenant_id=tenant_id, release_id=rel2.id, expected_version=2, reviewer_id="bob", reviewer_role="release_owner", decision="APPROVED")

    # Attempting to activate Release 2 while Release 1 is active raises ActiveReleaseConflictError
    with pytest.raises(ActiveReleaseConflictError):
        await release_svc.activate_canary(
            tenant_id=tenant_id,
            release_id=rel2.id,
            expected_version=3,
            canary_percentage=20,
            actor_id="bob",
            actor_role="release_owner",
        )
