"""Stage 11.2 Gate B: Unit and integration tests for durable guardrail outbox dispatcher."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.incident import IncidentStatus
from backend.domain.release import ReleaseState
from backend.persistence.models import Incident, Outbox, PolicyBundle, PolicyRelease
from backend.persistence.repositories import (
    IncidentRepository,
    OutboxRepository,
)
from backend.releases.guardrail_dispatcher import GuardrailOutboxDispatcher
from backend.releases.guardrails import ReleaseGuardrailService


async def _create_test_release(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    state: str = ReleaseState.CANARY.value,
) -> PolicyRelease:
    async with session_factory() as session:
        # Create bundles
        incumbent_bundle = PolicyBundle(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            bundle_name="incumbent-b1",
            bundle_sha256="0" * 64,
            signature="sig-incumbent",
            signature_key_id="key-1",
            base_policy_version="1.0.0",
            target_component="database",
            model_id="heuristic",
            model_version="1.0",
            tool_catalog_version="1.0",
            a2_grant_version="1.0",
            source_provenance_digest="0" * 64,
            status="SEALED",
            created_by="system",
        )
        candidate_bundle = PolicyBundle(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            bundle_name="candidate-b2",
            bundle_sha256="1" * 64,
            signature="sig-candidate",
            signature_key_id="key-1",
            base_policy_version="1.0.0",
            target_component="database",
            model_id="heuristic",
            model_version="1.0",
            tool_catalog_version="1.0",
            a2_grant_version="1.0",
            source_provenance_digest="1" * 64,
            status="SEALED",
            created_by="system",
        )
        session.add_all([incumbent_bundle, candidate_bundle])
        await session.flush()

        release = PolicyRelease(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            target_component="database",
            bundle_id=candidate_bundle.id,
            incumbent_bundle_id=incumbent_bundle.id,
            state=state,
            version=1,
            canary_percentage=20,
            rollout_salt=uuid.uuid4().hex,
            drafter_id="dev-1",
            kill_switch_active=False,
        )
        session.add(release)
        await session.commit()
        return release


@pytest.mark.asyncio
async def test_terminal_incident_enqueues_guardrail_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    inc_repo = IncidentRepository(session_factory)
    release = await _create_test_release(session_factory, isolated_tenant)

    incident_id = uuid.uuid4()
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"inc-key-{uuid.uuid4().hex[:6]}",
            target_id="database-shard-1",
            request_sha256="a" * 64,
            status_projection=IncidentStatus.INVESTIGATING.value,
            status_version=2,
            policy_release_id=release.id,
            canary_arm="CANDIDATE",
        )
        session.add(inc)
        await session.commit()

    # Advance incident to FAILED (terminal)
    await inc_repo.transition_status(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        target_status=IncidentStatus.FAILED,
        expected_version=2,
    )

    # Verify Outbox row exists with kind RELEASE_GUARDRAIL
    async with session_factory() as session:
        stmt = select(Outbox).where(
            Outbox.kind == "RELEASE_GUARDRAIL",
            Outbox.dedupe_key == f"guardrail:{release.id}",
        )
        outbox_row = (await session.execute(stmt)).scalar_one_or_none()
        assert outbox_row is not None
        assert outbox_row.delivered_at is None
        assert outbox_row.payload["release_id"] == str(release.id)
        assert outbox_row.payload["terminal_status"] == "FAILED"


@pytest.mark.asyncio
async def test_b1_dispatcher_ignores_guardrail_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    outbox_repo = OutboxRepository(session_factory)
    release = await _create_test_release(session_factory, isolated_tenant)

    # Insert a guardrail outbox row manually
    async with session_factory() as session:
        outbox = Outbox(
            kind="RELEASE_GUARDRAIL",
            dedupe_key=f"guardrail:{release.id}",
            payload={"tenant_id": str(isolated_tenant), "release_id": str(release.id)},
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(outbox)
        await session.commit()
        guardrail_outbox_id = outbox.id

    # Call B1 claim_batch (kind defaults to INCIDENT_START)
    b1_claimed = await outbox_repo.claim_batch(batch_size=50, lease_seconds=15.0)
    # Must NOT claim the RELEASE_GUARDRAIL row
    assert not any(item.outbox_id == guardrail_outbox_id for item in b1_claimed)


@pytest.mark.asyncio
async def test_guardrail_dispatcher_claims_and_delivers(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release = await _create_test_release(session_factory, isolated_tenant)
    dispatcher = GuardrailOutboxDispatcher(session_factory)

    # Insert a guardrail outbox row
    async with session_factory() as session:
        outbox = Outbox(
            kind="RELEASE_GUARDRAIL",
            dedupe_key=f"guardrail:{release.id}",
            payload={"tenant_id": str(isolated_tenant), "release_id": str(release.id)},
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(outbox)
        await session.commit()
        outbox_id = outbox.id

    # Run dispatcher dispatch_once
    processed = await dispatcher.dispatch_once()
    assert processed >= 1

    # Verify outbox row is now delivered
    async with session_factory() as session:
        row = await session.get(Outbox, outbox_id)
        assert row is not None
        assert row.delivered_at is not None
        assert row.lease_owner is None


@pytest.mark.asyncio
async def test_guardrail_dispatcher_triggers_rollback_on_invariant_breach(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release = await _create_test_release(session_factory, isolated_tenant)
    guardrail_svc = ReleaseGuardrailService(session_factory)
    dispatcher = GuardrailOutboxDispatcher(session_factory, guardrail_service=guardrail_svc)

    # Record a server-side invariant breach
    await guardrail_svc.record_invariant_breach(
        tenant_id=isolated_tenant,
        release_id=release.id,
        breach_detail="Negative control mutation detected in sandbox",
    )

    # Insert guardrail outbox task
    async with session_factory() as session:
        session.add(
            Outbox(
                kind="RELEASE_GUARDRAIL",
                dedupe_key=f"guardrail:{release.id}",
                payload={"tenant_id": str(isolated_tenant), "release_id": str(release.id)},
                available_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()

    # Dispatcher runs
    processed = await dispatcher.dispatch_once()
    assert processed >= 1

    # Check release is ROLLED_BACK
    async with session_factory() as session:
        rel = await session.get(PolicyRelease, release.id)
        assert rel is not None
        assert rel.state == ReleaseState.ROLLED_BACK.value
        assert rel.rollback_reason is not None
        assert "CLASS_1_INVARIANT_BREACH" in rel.rollback_reason


@pytest.mark.asyncio
async def test_guardrail_dispatcher_retry_on_transient_failure(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release = await _create_test_release(session_factory, isolated_tenant)

    # Mock guardrail service to raise transient RuntimeError
    mock_svc = AsyncMock()
    mock_svc.evaluate_and_enforce_guardrails.side_effect = RuntimeError("Database connection glitch")

    dispatcher = GuardrailOutboxDispatcher(
        session_factory,
        guardrail_service=mock_svc,
        base_backoff_seconds=2.0,
    )

    async with session_factory() as session:
        outbox = Outbox(
            kind="RELEASE_GUARDRAIL",
            dedupe_key=f"guardrail:{release.id}",
            payload={"tenant_id": str(isolated_tenant), "release_id": str(release.id)},
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(outbox)
        await session.commit()
        outbox_id = outbox.id

    # Dispatcher runs
    processed = await dispatcher.dispatch_once()
    assert processed == 0  # Failed, not delivered

    # Check outbox row: retry scheduled
    async with session_factory() as session:
        row = await session.get(Outbox, outbox_id)
        assert row is not None
        assert row.delivered_at is None
        assert row.attempt == 1
        assert "Database connection glitch" in (row.last_error or "")
        assert row.available_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_lease_expiry_allows_reclamation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    release = await _create_test_release(session_factory, isolated_tenant)
    outbox_repo = OutboxRepository(session_factory)

    # 1. Row available in past
    t0 = datetime.now(UTC) - timedelta(seconds=60)
    async with session_factory() as session:
        outbox = Outbox(
            kind="RELEASE_GUARDRAIL",
            dedupe_key=f"guardrail:{release.id}",
            payload={"tenant_id": str(isolated_tenant), "release_id": str(release.id)},
            available_at=t0,
        )
        session.add(outbox)
        await session.commit()

    # 2. Worker 1 claims with 5s lease
    claimed1 = await outbox_repo.claim_guardrail_batch(batch_size=10, lease_seconds=5.0, now=t0)
    assert len(claimed1) == 1
    assert claimed1[0].attempt == 1

    # 3. Immediately after t0 + 1s, worker 2 cannot claim (leased until t0 + 5s)
    claimed_mid = await outbox_repo.claim_guardrail_batch(batch_size=10, lease_seconds=5.0, now=t0 + timedelta(seconds=1))
    assert len(claimed_mid) == 0

    # 4. At t0 + 6s (lease expired, worker 1 crashed), worker 2 successfully re-claims
    claimed2 = await outbox_repo.claim_guardrail_batch(batch_size=10, lease_seconds=5.0, now=t0 + timedelta(seconds=6))
    assert len(claimed2) == 1
    assert claimed2[0].attempt == 2
