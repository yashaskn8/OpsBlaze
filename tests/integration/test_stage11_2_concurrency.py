"""Integration and Concurrency Race Tests for Stage 11.2 (F-3.1).

Verifies:
- Monotonic lock acquisition between OutboxRepository.mark_delivered()
  and IncidentRepository.transition_status() to eliminate PostgreSQL deadlocks (40P01).
- Concurrent incident cancellation and outbox delivery under heavy contention.
- SKIP LOCKED concurrency semantics for guardrail and shadow outbox claim batches.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.incident import IncidentStatus
from backend.persistence.models import Incident, Outbox
from backend.persistence.repositories import (
    OUTBOX_KIND_GUARDRAIL,
    OUTBOX_KIND_SHADOW,
    IncidentRepository,
    OutboxRepository,
)

pytestmark = pytest.mark.postgres


async def _admit_test_incident(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    client_prefix: str = "race-f31",
) -> tuple[uuid.UUID, int]:
    """Helper to admit an incident in ACCEPTED state with an undelivered outbox row."""
    inc_repo = IncidentRepository(session_factory)
    client_key = f"{client_prefix}-{uuid.uuid4()}"
    outcome = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=client_key,
        target_id="k8s:pod:service-alpha",
        request_sha256="a" * 64,
    )
    inc_id = outcome.response.id

    async with session_factory() as session:
        outbox_row = (
            await session.execute(
                select(Outbox).where(Outbox.incident_id == inc_id).limit(1)
            )
        ).scalar_one()
        outbox_id = outbox_row.id

    return inc_id, outbox_id


@pytest.mark.asyncio
async def test_monotonic_locking_serializes_without_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verifies that when both sessions acquire locks monotonically (Incident -> Outbox),
    the transactions serialize cleanly without any deadlock.
    """
    inc_id, outbox_id = await _admit_test_incident(session_factory, isolated_tenant)
    start_barrier = asyncio.Barrier(2)

    async def monotonic_worker(worker_id: int) -> None:
        async with session_factory() as session:
            await start_barrier.wait()
            # 1. Monotonic Lock: Incident FIRST
            inc = (
                await session.execute(
                    select(Incident).where(Incident.id == inc_id).with_for_update()
                )
            ).scalar_one()

            # 2. Monotonic Lock: Outbox SECOND
            out = (
                await session.execute(
                    select(Outbox).where(Outbox.id == outbox_id).with_for_update()
                )
            ).scalar_one()

            if inc.status_projection == IncidentStatus.ACCEPTED.value:
                if worker_id == 1:
                    inc.status_projection = IncidentStatus.DISPATCH_PENDING.value
                else:
                    inc.status_projection = IncidentStatus.CANCELLED.value
                inc.status_version += 1
            elif inc.status_projection == IncidentStatus.DISPATCH_PENDING.value and worker_id == 2:
                inc.status_projection = IncidentStatus.CANCELLED.value
                inc.status_version += 1

            if out.delivered_at is None:
                out.delivered_at = datetime.now(UTC)

            await session.commit()

    # Both workers run concurrently
    results = await asyncio.gather(
        monotonic_worker(1),
        monotonic_worker(2),
        return_exceptions=True,
    )

    for res in results:
        assert not isinstance(res, Exception), f"Unexpected exception: {res}"

    inc_repo = IncidentRepository(session_factory)
    snapshot = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert snapshot.status == IncidentStatus.CANCELLED
    assert snapshot.outbox_delivered is True


@pytest.mark.asyncio
async def test_concurrent_mark_delivered_and_cancellation_race(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Integration race test:
    Executes OutboxRepository.mark_delivered() and IncidentRepository.transition_status(CANCELLED)
    simultaneously behind an asyncio.Barrier(2) on the same incident.
    Verifies 0 deadlocks, clean serialization, and strict terminal state preservation.
    """
    inc_repo = IncidentRepository(session_factory)
    outbox_repo = OutboxRepository(session_factory)

    inc_id, outbox_id = await _admit_test_incident(session_factory, isolated_tenant)
    barrier = asyncio.Barrier(2)

    async def run_mark_delivered(oid: int = outbox_id, iid: uuid.UUID = inc_id) -> bool:
        await barrier.wait()
        return await outbox_repo.mark_delivered(
            outbox_id=oid,
            incident_id=iid,
            tenant_id=isolated_tenant,
        )

    async def run_cancellation(iid: uuid.UUID = inc_id) -> None:
        await barrier.wait()
        await inc_repo.transition_status(
            tenant_id=isolated_tenant,
            incident_id=iid,
            target_status=IncidentStatus.CANCELLED,
            allow_cancelled_noop=True,
        )

    outcomes = await asyncio.gather(
        run_mark_delivered(),
        run_cancellation(),
        return_exceptions=True,
    )

    for outcome in outcomes:
        assert not isinstance(outcome, Exception), f"Concurrent execution failed with: {outcome}"

    snapshot = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert snapshot.status == IncidentStatus.CANCELLED
    assert snapshot.outbox_delivered is True
    assert snapshot.status_version in (2, 3)


@pytest.mark.asyncio
async def test_high_contention_concurrent_delivery_and_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Stress race: N=10 concurrent contenders (5 deliveries, 5 cancellations) racing
    on the exact same incident row.
    """
    inc_repo = IncidentRepository(session_factory)
    outbox_repo = OutboxRepository(session_factory)

    inc_id, outbox_id = await _admit_test_incident(session_factory, isolated_tenant)
    n_contenders = 10
    barrier = asyncio.Barrier(n_contenders)

    async def delivery_task(oid: int = outbox_id, iid: uuid.UUID = inc_id) -> bool:
        await barrier.wait()
        return await outbox_repo.mark_delivered(
            outbox_id=oid,
            incident_id=iid,
            tenant_id=isolated_tenant,
        )

    async def cancel_task(iid: uuid.UUID = inc_id) -> None:
        await barrier.wait()
        await inc_repo.transition_status(
            tenant_id=isolated_tenant,
            incident_id=iid,
            target_status=IncidentStatus.CANCELLED,
            allow_cancelled_noop=True,
        )

    tasks = [
        delivery_task() if i % 2 == 0 else cancel_task()
        for i in range(n_contenders)
    ]

    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for outcome in outcomes:
        assert not isinstance(outcome, DBAPIError), f"DBAPIError encountered: {outcome}"

    snapshot = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert snapshot.status == IncidentStatus.CANCELLED
    assert snapshot.outbox_delivered is True


@pytest.mark.asyncio
async def test_concurrent_guardrail_outbox_claims_skip_locked(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify that concurrent claim_guardrail_batch calls use SKIP LOCKED to partition
    work disjointly across concurrent workers without duplicate claims.
    """
    incident_repo = IncidentRepository(session_factory)
    outbox_repo = OutboxRepository(session_factory)
    num_items = 6
    now = datetime.now(UTC)

    async with session_factory() as session:
        for idx in range(num_items):
            admit = await incident_repo.admit_incident(
                tenant_id=isolated_tenant,
                client_key=f"gr-concurr-{idx}-{uuid.uuid4().hex[:6]}",
                target_id="target-concurrency-gr",
                request_sha256="2" * 64,
            )
            item = Outbox(
                incident_id=admit.response.id,
                kind=OUTBOX_KIND_GUARDRAIL,
                payload={"incident_id": str(admit.response.id), "policy_release_id": f"rel-{idx}"},
                available_at=now,
                dedupe_key=f"guardrail-test-skip-locked-{uuid.uuid4().hex[:8]}",
            )
            session.add(item)
        await session.commit()

    num_workers = 3
    barrier = asyncio.Barrier(num_workers)

    async def _claim_worker() -> list[int]:
        await barrier.wait()
        claimed = await outbox_repo.claim_guardrail_batch(
            batch_size=2,
            lease_seconds=30,
            now=now,
        )
        return [c.outbox_id for c in claimed]

    tasks = [_claim_worker() for _ in range(num_workers)]
    claimed_batches = await asyncio.gather(*tasks, return_exceptions=False)

    all_claimed: list[int] = []
    for batch in claimed_batches:
        assert len(batch) == len(set(batch))
        all_claimed.extend(batch)

    assert len(all_claimed) == len(set(all_claimed))


@pytest.mark.asyncio
async def test_concurrent_shadow_outbox_claims_skip_locked(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify that concurrent claim_shadow_batch calls use SKIP LOCKED to partition
    work disjointly across concurrent workers without duplicate claims.
    """
    incident_repo = IncidentRepository(session_factory)
    outbox_repo = OutboxRepository(session_factory)
    num_items = 6
    now = datetime.now(UTC)

    async with session_factory() as session:
        for idx in range(num_items):
            admit = await incident_repo.admit_incident(
                tenant_id=isolated_tenant,
                client_key=f"sh-concurr-{idx}-{uuid.uuid4().hex[:6]}",
                target_id="target-concurrency-sh",
                request_sha256="3" * 64,
            )
            item = Outbox(
                incident_id=admit.response.id,
                kind=OUTBOX_KIND_SHADOW,
                payload={"incident_id": str(admit.response.id), "candidate_policy_id": f"cand-{idx}"},
                available_at=now,
                dedupe_key=f"shadow-test-skip-locked-{uuid.uuid4().hex[:8]}",
            )
            session.add(item)
        await session.commit()

    num_workers = 3
    barrier = asyncio.Barrier(num_workers)

    async def _claim_worker() -> list[int]:
        await barrier.wait()
        claimed = await outbox_repo.claim_shadow_batch(
            batch_size=2,
            lease_seconds=30,
            now=now,
        )
        return [c.outbox_id for c in claimed]

    tasks = [_claim_worker() for _ in range(num_workers)]
    claimed_batches = await asyncio.gather(*tasks, return_exceptions=False)

    all_claimed: list[int] = []
    for batch in claimed_batches:
        assert len(batch) == len(set(batch))
        all_claimed.extend(batch)

    assert len(all_claimed) == len(set(all_claimed))
