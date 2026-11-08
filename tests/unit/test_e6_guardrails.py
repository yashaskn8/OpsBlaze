"""Unit tests for Feature E6 — Safety Guardrails and Automated Rollback."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.domain.incident import IncidentStatus
from backend.domain.release import ReleaseState
from backend.persistence.models import Incident, Outbox, Tenant
from backend.persistence.repositories import IncidentRepository
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.guardrail_dispatcher import ReleaseGuardrailDispatcher
from backend.releases.guardrails import ReleaseGuardrailService
from backend.releases.release_service import PolicyReleaseService


@pytest.fixture
async def active_canary_release(session_factory):
    tenant_id = uuid.uuid4()
    bundle_svc = PolicyBundleService(session_factory)
    release_svc = PolicyReleaseService(session_factory)

    async with session_factory() as session:
        t = Tenant(id=tenant_id, name=f"test-tenant-guardrail-{tenant_id}")
        session.add(t)
        await session.commit()

    incumbent = await bundle_svc.ensure_bootstrap_incumbent(tenant_id=tenant_id, target_component="investigator")
    candidate = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="candidate-guardrail",
        base_policy_version="v2.0.0",
        target_component="investigator",
        created_by="drafter",
        model_id="gpt-4o",
        model_version="2026-11",
        tool_catalog_version="v2",
        a2_grant_version="v2",
        source_provenance_digest="3" * 64,
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
    rel = await release_svc.activate_canary(tenant_id=tenant_id, release_id=rel.id, expected_version=3, canary_percentage=20, actor_id="reviewer", actor_role="release_owner")

    return tenant_id, rel


@pytest.mark.asyncio
async def test_class_1_zero_tolerance_invariant_breach_triggers_immediate_rollback(
    session_factory, active_canary_release
) -> None:
    """A single unauthorized mutation / invariant breach triggers instantaneous automated rollback."""
    tenant_id, release = active_canary_release
    release_svc = PolicyReleaseService(session_factory)
    guardrail_svc = ReleaseGuardrailService(session_factory, release_svc)

    await guardrail_svc.record_invariant_breach(
        tenant_id=tenant_id,
        release_id=release.id,
        breach_detail="Unauthorized mutation attempt detected in read-only investigation.",
    )

    rolled_back, reason = await guardrail_svc.evaluate_and_enforce_guardrails(
        tenant_id=tenant_id,
        release_id=release.id,
    )

    assert rolled_back is True
    assert "CLASS_1_INVARIANT_BREACH" in reason

    # Verify release state in DB is now ROLLED_BACK
    updated_release = await release_svc.get_release(tenant_id=tenant_id, release_id=release.id)
    assert updated_release.state == ReleaseState.ROLLED_BACK.value
    assert updated_release.canary_percentage == 0
    assert "Unauthorized mutation attempt" in (updated_release.rollback_reason or "")


@pytest.mark.asyncio
async def test_class_2_statistical_rate_breach_triggers_rollback(
    session_factory, active_canary_release
) -> None:
    """When N >= 30, Candidate Error Rate > 5% and Relative Risk > 1.5, triggers rollback."""
    tenant_id, release = active_canary_release
    release_svc = PolicyReleaseService(session_factory)
    guardrail_svc = ReleaseGuardrailService(session_factory, release_svc)

    # Insert 50 candidate incidents (6 failures = 12% error rate) and 50 incumbent incidents (1 failure = 2% error rate)
    async with session_factory() as session:
        for i in range(50):
            status = "FAILED" if i < 6 else "ACCEPTED"
            session.add(Incident(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_key=f"cand-{i}",
                request_sha256="abc",
                target_id="target-01",
                status_projection=status,
                status_version=1,
                policy_release_id=release.id,
                canary_arm="CANDIDATE",
            ))
        for i in range(50):
            status = "FAILED" if i < 1 else "ACCEPTED"
            session.add(Incident(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_key=f"inc-{i}",
                request_sha256="abc",
                target_id="target-01",
                status_projection=status,
                status_version=1,
                policy_release_id=release.id,
                canary_arm="INCUMBENT",
            ))
        await session.commit()

    metrics = await guardrail_svc.compute_canary_metrics(tenant_id=tenant_id, release_id=release.id)
    assert metrics.candidate_sample_size == 50
    assert metrics.incumbent_sample_size == 50
    assert metrics.candidate_error_rate == 0.12
    assert metrics.incumbent_error_rate == 0.02
    assert metrics.relative_risk is not None and metrics.relative_risk > 1.5
    assert metrics.recommendation == "ROLLBACK_REQUIRED_STATISTICAL_RATE"

    rolled_back, reason = await guardrail_svc.evaluate_and_enforce_guardrails(
        tenant_id=tenant_id,
        release_id=release.id,
    )
    assert rolled_back is True
    assert "CLASS_2_STATISTICAL_RATE_BREACH" in reason

    updated_release = await release_svc.get_release(tenant_id=tenant_id, release_id=release.id)
    assert updated_release.state == ReleaseState.ROLLED_BACK.value



@pytest.mark.asyncio
async def test_terminal_incident_transition_enqueues_retryable_guardrail_outbox(
    session_factory, active_canary_release
) -> None:
    """The guardrail event is committed atomically and acknowledged by a separate dispatcher."""
    tenant_id, release = active_canary_release
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Incident(
                id=incident_id,
                tenant_id=tenant_id,
                client_key=f"guardrail-outbox-{incident_id}",
                request_sha256="e" * 64,
                target_id="target-guardrail",
                status_projection=IncidentStatus.ACCEPTED.value,
                status_version=1,
                policy_release_id=release.id,
                canary_arm="CANDIDATE",
            )
        )
        await session.commit()

    repo = IncidentRepository(session_factory)
    await repo.transition_status(
        tenant_id=tenant_id,
        incident_id=incident_id,
        target_status=IncidentStatus.CANCELLED,
    )

    async with session_factory() as session:
        queued = (
            await session.execute(
                select(Outbox).where(
                    Outbox.incident_id == incident_id,
                    Outbox.kind == "RELEASE_GUARDRAIL",
                )
            )
        ).scalar_one()
        assert queued.delivered_at is None
        assert queued.dedupe_key == f"guardrail:{incident_id}:2"

    dispatcher = ReleaseGuardrailDispatcher(session_factory)
    assert await dispatcher.dispatch_once()

    async with session_factory() as session:
        completed = await session.get(Outbox, queued.id)
        assert completed is not None
        assert completed.delivered_at is not None
        assert completed.last_error is None
