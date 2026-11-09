"""Stage 11.2 Gate C: Unit and integration tests for isolated candidate shadow execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.incident import IncidentStatus
from backend.domain.release import CanaryArm, ReleaseState
from backend.persistence.models import (
    ApprovalRequest,
    EffectReceipt,
    Incident,
    Outbox,
    PolicyBundle,
    PolicyRelease,
    ReleaseEventAudit,
    ResourceReservation,
)
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.router_service import PolicyRouterService
from backend.releases.shadow_dispatcher import ShadowOutboxDispatcher
from backend.releases.shadow_executor import ShadowExecutor


async def _setup_sealed_bundles_and_release(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    state: str = ReleaseState.SHADOW.value,
) -> tuple[PolicyRelease, PolicyBundle, PolicyBundle]:
    bundle_svc = PolicyBundleService(session_factory)

    # 1. Create and seal incumbent bundle
    incumbent = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="incumbent-db-policy",
        base_policy_version="1.0.0",
        target_component="database",
        model_id="heuristic",
        model_version="1.0",
        tool_catalog_version="1.0",
        a2_grant_version="1.0",
        source_provenance_digest="0" * 64,
        prompt_templates={"default_action": "restart_replica"},
        created_by="system",
    )
    incumbent = await bundle_svc.seal_bundle(tenant_id=tenant_id, bundle_id=incumbent.id)

    # 2. Create and seal candidate bundle with divergent proposed action
    candidate = await bundle_svc.create_bundle(
        tenant_id=tenant_id,
        bundle_name="candidate-db-policy-v2",
        base_policy_version="1.0.0",
        target_component="database",
        model_id="heuristic",
        model_version="2.0",
        tool_catalog_version="1.0",
        a2_grant_version="1.0",
        source_provenance_digest="1" * 64,
        proposal_diff={"proposed_action": "failover_replica_pool"},
        created_by="engineer-alice",
    )
    candidate = await bundle_svc.seal_bundle(tenant_id=tenant_id, bundle_id=candidate.id)

    # 3. Create release in SHADOW state
    async with session_factory() as session:
        release = PolicyRelease(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            version=1,
            target_component="database",
            bundle_id=candidate.id,
            incumbent_bundle_id=incumbent.id,
            state=state,
            canary_percentage=0,
            rollout_salt=uuid.uuid4().hex,
            drafter_id="engineer-alice",
            kill_switch_active=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(release)
        await session.commit()
        await session.refresh(release)

    return release, candidate, incumbent


@pytest.mark.asyncio
async def test_shadow_routing_enqueues_outbox_event(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, candidate, incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    router_svc = PolicyRouterService(session_factory)

    # Create an incoming incident
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="b" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
        )
        session.add(inc)
        await session.commit()

    # Route incident
    arm, bundle_id, rel_id, _hash_val = await router_svc.route_incident(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        target_id="database-shard-1",
        target_component="database",
    )

    # Live incident is served by INCUMBENT arm
    assert arm == CanaryArm.INCUMBENT
    assert bundle_id == incumbent.id
    assert rel_id == release.id

    # Verify atomic Outbox enqueue for RELEASE_SHADOW_EVALUATION
    async with session_factory() as session:
        stmt = select(Outbox).where(
            Outbox.kind == "RELEASE_SHADOW_EVALUATION",
            Outbox.dedupe_key == f"shadow:{release.id}:{incident_id}",
        )
        outbox_row = (await session.execute(stmt)).scalar_one_or_none()
        assert outbox_row is not None
        assert outbox_row.delivered_at is None
        assert outbox_row.payload["candidate_bundle_id"] == str(candidate.id)
        assert outbox_row.payload["incumbent_bundle_id"] == str(incumbent.id)


@pytest.mark.asyncio
async def test_shadow_executor_runs_and_records_observation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, _candidate, _incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    executor = ShadowExecutor(session_factory)

    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="c" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
            policy_release_id=release.id,
        )
        session.add(inc)
        await session.commit()

    result = await executor.execute_shadow_evaluation(
        tenant_id=isolated_tenant,
        release_id=release.id,
        incident_id=incident_id,
    )

    assert result.status == "COMPLETED"
    assert result.divergence is True
    assert result.candidate_action == "failover_replica_pool"
    assert result.incumbent_action == "restart_replica"

    # Verify audit record created with zero consequential authority
    async with session_factory() as session:
        stmt = select(ReleaseEventAudit).where(
            ReleaseEventAudit.tenant_id == isolated_tenant,
            ReleaseEventAudit.release_id == release.id,
            ReleaseEventAudit.event_type == "SHADOW_OBSERVATION",
        )
        audit = (await session.execute(stmt)).scalar_one_or_none()
        assert audit is not None
        meta = audit.metadata_redacted
        assert meta["consequential_authority"] is False
        assert meta["divergence"] is True
        assert meta["candidate_action"] == "failover_replica_pool"
        assert meta["incumbent_action"] == "restart_replica"

        # Explicit proof: ZERO consequential mutations occurred
        b2_count = (await session.execute(
            select(ResourceReservation).where(ResourceReservation.tenant_id == isolated_tenant)
        )).scalars().all()
        assert len(b2_count) == 0

        b3_count = (await session.execute(
            select(EffectReceipt).where(EffectReceipt.tenant_id == isolated_tenant)
        )).scalars().all()
        assert len(b3_count) == 0

        p2_count = (await session.execute(
            select(ApprovalRequest).where(ApprovalRequest.tenant_id == isolated_tenant)
        )).scalars().all()
        assert len(p2_count) == 0


@pytest.mark.asyncio
async def test_shadow_executor_integrity_failure_trips_invariant_breach(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, candidate, _incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    executor = ShadowExecutor(session_factory)

    # Corrupt candidate signature
    async with session_factory() as session:
        cand = await session.get(PolicyBundle, candidate.id)
        assert cand is not None
        cand.signature = "tampered-invalid-signature"
        await session.commit()

    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="d" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
            policy_release_id=release.id,
        )
        session.add(inc)
        await session.commit()

    result = await executor.execute_shadow_evaluation(
        tenant_id=isolated_tenant,
        release_id=release.id,
        incident_id=incident_id,
    )

    assert result.status == "ABORTED_INTEGRITY_FAILURE"

    # Verify Class 1 invariant breach was recorded
    async with session_factory() as session:
        stmt = select(ReleaseEventAudit).where(
            ReleaseEventAudit.tenant_id == isolated_tenant,
            ReleaseEventAudit.release_id == release.id,
            ReleaseEventAudit.event_type == "INVARIANT_BREACH",
        )
        audit = (await session.execute(stmt)).scalar_one_or_none()
        assert audit is not None
        assert "Bundle verification failed in shadow" in audit.metadata_redacted["detail"]


@pytest.mark.asyncio
async def test_shadow_executor_aborts_on_kill_switch_or_state_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, _candidate, _incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    executor = ShadowExecutor(session_factory)

    # 1. Kill switch active
    async with session_factory() as session:
        rel = await session.get(PolicyRelease, release.id)
        assert rel is not None
        rel.kill_switch_active = True
        await session.commit()

    incident_id = uuid.uuid4()
    res1 = await executor.execute_shadow_evaluation(
        tenant_id=isolated_tenant,
        release_id=release.id,
        incident_id=incident_id,
    )
    assert res1.status == "ABORTED_KILL_SWITCH"

    # 2. State mismatch (e.g. release advanced to CANARY)
    async with session_factory() as session:
        rel = await session.get(PolicyRelease, release.id)
        assert rel is not None
        rel.kill_switch_active = False
        rel.state = ReleaseState.CANARY.value
        await session.commit()

    res2 = await executor.execute_shadow_evaluation(
        tenant_id=isolated_tenant,
        release_id=release.id,
        incident_id=incident_id,
    )
    assert res2.status == "ABORTED_STATE_MISMATCH"


@pytest.mark.asyncio
async def test_shadow_executor_mid_flight_kill_switch_discards_candidate_findings(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, _candidate, _incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    executor = ShadowExecutor(session_factory)

    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="e" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
            policy_release_id=release.id,
        )
        session.add(inc)
        await session.commit()

    # Simulate mid-flight race: activate kill switch right before lock recheck
    # We can do this by tripping kill switch in database prior to step 4
    # To test step 4 directly, we can activate kill switch on the release:
    # We patch verify_bundle_integrity side effect to activate kill switch during execution!
    original_verify = executor._bundle_service.verify_bundle_integrity

    async def trip_kill_switch_during_eval(*args: Any, **kwargs: Any) -> bool:
        async with session_factory() as session:
            rel = await session.get(PolicyRelease, release.id)
            if rel:
                rel.kill_switch_active = True
                await session.commit()
        return await original_verify(*args, **kwargs)

    with patch.object(executor._bundle_service, "verify_bundle_integrity", side_effect=trip_kill_switch_during_eval):
        result = await executor.execute_shadow_evaluation(
            tenant_id=isolated_tenant,
            release_id=release.id,
            incident_id=incident_id,
        )

    assert result.status == "DISCARDED_KILL_SWITCH"

    # Confirm discarded audit event exists
    async with session_factory() as session:
        stmt = select(ReleaseEventAudit).where(
            ReleaseEventAudit.tenant_id == isolated_tenant,
            ReleaseEventAudit.release_id == release.id,
            ReleaseEventAudit.event_type == "SHADOW_RESULT_DISCARDED_KILL_SWITCH",
        )
        audit = (await session.execute(stmt)).scalar_one_or_none()
        assert audit is not None

        # Confirm NO SHADOW_OBSERVATION was written
        obs_stmt = select(ReleaseEventAudit).where(
            ReleaseEventAudit.tenant_id == isolated_tenant,
            ReleaseEventAudit.release_id == release.id,
            ReleaseEventAudit.event_type == "SHADOW_OBSERVATION",
        )
        obs_audit = (await session.execute(obs_stmt)).scalar_one_or_none()
        assert obs_audit is None


@pytest.mark.asyncio
async def test_shadow_dispatcher_claims_and_delivers(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release, candidate, incumbent = await _setup_sealed_bundles_and_release(
        session_factory, isolated_tenant, state=ReleaseState.SHADOW.value
    )
    dispatcher = ShadowOutboxDispatcher(session_factory)

    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="f" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
            policy_release_id=release.id,
        )
        session.add(inc)

        outbox = Outbox(
            kind="RELEASE_SHADOW_EVALUATION",
            dedupe_key=f"shadow:{release.id}:{incident_id}",
            payload={
                "tenant_id": str(isolated_tenant),
                "release_id": str(release.id),
                "incident_id": str(incident_id),
                "candidate_bundle_id": str(candidate.id),
                "incumbent_bundle_id": str(incumbent.id),
            },
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(outbox)
        await session.commit()
        outbox_id = outbox.id

    processed = await dispatcher.dispatch_once()
    assert processed == 1

    # Verify outbox row marked delivered
    async with session_factory() as session:
        row = await session.get(Outbox, outbox_id)
        assert row is not None
        assert row.delivered_at is not None
        assert row.lease_owner is None
