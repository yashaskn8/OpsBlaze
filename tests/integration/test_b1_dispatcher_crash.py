"""Phase 3.3 — Transactional Outbox Dispatcher crash-window and multi-dispatcher concurrency tests."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.domain.incident import (
    IncidentStatus,
    WorkflowIncidentInput,
    make_incident_workflow_id,
)
from backend.observability.metrics import metrics
from backend.persistence.models import Incident, Outbox
from backend.persistence.repositories import IncidentRepository, OutboxRepository
from backend.workflows.dispatcher import OutboxDispatcher

pytestmark = pytest.mark.postgres


class FakeTemporalStarter:
    """Controllable Temporal starter recording physical start attempts vs logical workflow creations."""

    def __init__(self) -> None:
        self.started_workflows: dict[str, WorkflowIncidentInput] = {}
        self.physical_attempts: list[str] = []
        self.fail_before_accept: bool = False
        self.lose_ack_after_accept: bool = False

    async def start_incident_workflow(self, wf_id: str, wf_input: WorkflowIncidentInput) -> str:
        self.physical_attempts.append(wf_id)
        if self.fail_before_accept:
            raise RuntimeError("Simulated Temporal connection refused before accept")

        already_existed = wf_id in self.started_workflows
        if not already_existed:
            self.started_workflows[wf_id] = wf_input

        if self.lose_ack_after_accept:
            # Temporal accepted the workflow, but the network ack is lost / dispatcher crashes!
            raise OSError("Simulated lost network acknowledgment after Temporal accepted start")

        if already_existed:
            raise WorkflowAlreadyStartedError(wf_id, "IncidentWorkflow")

        return wf_id


async def _get_outbox_for_incident(
    session_factory: async_sessionmaker[AsyncSession], incident_id: uuid.UUID
) -> Outbox:
    async with session_factory() as session:
        row = (
            await session.execute(select(Outbox).where(Outbox.incident_id == incident_id))
        ).scalar_one()
        return row


async def test_crash_windows_1_to_4_before_claim_after_claim_and_during_temporal_start(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Test crash windows:
    1. Crash before outbox claim.
    2. Crash immediately after outbox claim (before Temporal RPC).
    3. Crash before Temporal start.
    4. Failure during Temporal start (Temporal unavailable).
    Verify pending work is never lost and recovers once lease/backoff expires.
    """
    inc_repo = IncidentRepository(session_factory)
    outbox_repo = OutboxRepository(session_factory)
    starter = FakeTemporalStarter()

    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"cw-1-4-{uuid.uuid4()}",
        target_id="sim-db-1",
        request_sha256="a" * 64,
    )
    inc_id = outcome.response.id
    t0 = datetime.now(UTC)

    # Window 1: Crash before claim -> outbox remains attempt=0, delivered_at=None
    ob = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob.attempt == 0
    assert ob.delivered_at is None

    # Window 2 & 3: Claim row in short tx, then dispatcher process crashes before calling Temporal
    claimed = await outbox_repo.claim_batch(batch_size=10, lease_seconds=5.0, now=t0)
    assert any(c.incident_id == inc_id for c in claimed)
    ob_after_claim = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob_after_claim.attempt == 1
    assert ob_after_claim.delivered_at is None

    # While lease is active (t0 + 1s), another dispatcher cannot reclaim it
    reclaim_early = await outbox_repo.claim_batch(
        batch_size=10, lease_seconds=5.0, now=t0 + timedelta(seconds=1)
    )
    assert not any(c.incident_id == inc_id for c in reclaim_early)

    # Window 4: After lease expires (t0 + 6s), dispatcher reclaims it, but Temporal fails before accept
    starter.fail_before_accept = True
    dispatcher = OutboxDispatcher(
        session_factory,
        starter,
        lease_seconds=5.0,
        base_backoff_seconds=2.0,
    )
    delivered_count = await dispatcher.dispatch_once(now=t0 + timedelta(seconds=6))
    assert delivered_count == 0
    assert len(starter.started_workflows) == 0

    ob_after_fail = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob_after_fail.attempt == 2
    assert ob_after_fail.delivered_at is None

    # Recovery: Temporal comes back up at t0 + 15s
    starter.fail_before_accept = False
    delivered_recovered = await dispatcher.dispatch_once(now=t0 + timedelta(seconds=15))
    assert delivered_recovered >= 1

    ob_final = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob_final.delivered_at is not None
    wf_id = make_incident_workflow_id(isolated_tenant, inc_id)
    assert wf_id in starter.started_workflows


async def test_critical_scenario_crash_windows_5_to_9_lost_ack_and_reclaim_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Critical Scenario from Mandate Section 4.3 & 5.3 (Windows 5, 6, 7, 8, 9):
    1. Dispatcher claims outbox row.
    2. Temporal accepts workflow start.
    3. Dispatcher loses acknowledgment / crashes before delivered_at update.
    4. Outbox row is reclaimed after backoff/lease.
    5. Dispatcher retries using the SAME workflow ID.
    6. Existing workflow is identified (WorkflowAlreadyStartedError handled idempotently).
    7. Outbox intent is marked delivered and incident status projected to DISPATCH_PENDING.
    8. No second logical incident or workflow is created.
    """
    inc_repo = IncidentRepository(session_factory)
    starter = FakeTemporalStarter()

    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"cw-critical-{uuid.uuid4()}",
        target_id="sim-critical-target",
        request_sha256="b" * 64,
    )
    inc_id = outcome.response.id
    expected_wf_id = make_incident_workflow_id(isolated_tenant, inc_id)
    t0 = datetime.now(UTC)

    # Step 1-4: Temporal accepts start, but ack is lost before mark_delivered
    starter.lose_ack_after_accept = True
    dispatcher_1 = OutboxDispatcher(
        session_factory,
        starter,
        lease_seconds=5.0,
        base_backoff_seconds=1.0,
    )
    delivered_1 = await dispatcher_1.dispatch_once(now=t0)
    assert delivered_1 == 0
    # Workflow WAS created in Temporal
    assert list(starter.started_workflows.keys()) == [expected_wf_id]
    # Outbox is still undelivered in PostgreSQL
    ob_mid = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob_mid.delivered_at is None
    assert ob_mid.attempt == 1

    # Window 7: Simulate DB failure during mark_delivered on retry, then full recovery
    starter.lose_ack_after_accept = False
    orig_mark = dispatcher_1._outbox_repo.mark_delivered

    async def failing_mark_delivered(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("UPDATE outbox", {}, Exception("crash during delivered update"))

    dispatcher_1._outbox_repo.mark_delivered = failing_mark_delivered  # type: ignore[method-assign]
    with pytest.raises(OperationalError):
        await dispatcher_1.dispatch_once(now=t0 + timedelta(seconds=10))

    # Restore mark_delivered and run restarted dispatcher instance 2 after lease expiry
    dispatcher_1._outbox_repo.mark_delivered = orig_mark  # type: ignore[method-assign]
    dispatcher_2 = OutboxDispatcher(
        session_factory,
        starter,
        lease_seconds=5.0,
        base_backoff_seconds=1.0,
    )
    delivered_2 = await dispatcher_2.dispatch_once(now=t0 + timedelta(seconds=20))
    assert delivered_2 >= 1

    # Verify postconditions:
    # - 3 physical start calls were made with the exact same workflow ID
    assert starter.physical_attempts.count(expected_wf_id) == 3
    # - Only 1 logical workflow exists
    assert len(starter.started_workflows) == 1
    # - Outbox is marked delivered
    ob_done = await _get_outbox_for_incident(session_factory, inc_id)
    assert ob_done.delivered_at is not None
    # - Incident status advanced to DISPATCH_PENDING (version 2)
    inc_snap = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert inc_snap.status == IncidentStatus.DISPATCH_PENDING
    assert inc_snap.status_version == 2
    assert inc_snap.outbox_delivered is True

    # Window 8: Subsequent dispatcher pass after delivered_at is set does not re-claim
    delivered_3 = await dispatcher_2.dispatch_once(now=t0 + timedelta(seconds=30))
    assert starter.physical_attempts.count(expected_wf_id) == 3
    assert delivered_3 == 0


async def test_window_10_two_dispatchers_claiming_concurrently_skip_locked(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Two dispatcher instances claiming work concurrently over 10 pending incidents:
    FOR UPDATE SKIP LOCKED ensures disjoint claims and zero duplicate logical workflows.
    """
    inc_repo = IncidentRepository(session_factory)
    starter = FakeTemporalStarter()

    admitted_ids: list[uuid.UUID] = []
    for i in range(10):
        outcome = await inc_repo.admit_incident(
            tenant_id=isolated_tenant,
            client_key=f"conc-disp-{i}-{uuid.uuid4()}",
            target_id=f"sim-target-{i}",
            request_sha256=f"{i:064d}",
        )
        admitted_ids.append(outcome.response.id)

    d1 = OutboxDispatcher(session_factory, starter, batch_size=6, max_concurrency=4)
    d2 = OutboxDispatcher(session_factory, starter, batch_size=6, max_concurrency=4)

    barrier = asyncio.Event()

    async def run_disp(d: OutboxDispatcher) -> int:
        await barrier.wait()
        return await d.dispatch_once()

    t1 = asyncio.create_task(run_disp(d1))
    t2 = asyncio.create_task(run_disp(d2))
    await asyncio.sleep(0.01)
    barrier.set()
    c1, c2 = await asyncio.gather(t1, t2)

    assert c1 + c2 >= 10
    for inc_id in admitted_ids:
        wf_id = make_incident_workflow_id(isolated_tenant, inc_id)
        assert wf_id in starter.started_workflows
        # Because SKIP LOCKED prevents simultaneous duplicate claims, each incident was started once
        assert starter.physical_attempts.count(wf_id) == 1

    # Verify only 1 incident row per admitted ID
    async with session_factory() as session:
        total_inc = (
            (
                await session.execute(
                    select(Incident.id).where(Incident.tenant_id == isolated_tenant)
                )
            )
            .scalars()
            .all()
        )
        assert len(total_inc) == 10


async def test_stuck_outbox_detection_emits_stuck_event_and_metric(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify an outbox entry exceeding stuck_attempt_threshold or stuck_age_seconds
    emits `outbox.stuck` and updates `stuck_workflow_age_seconds`.
    """
    inc_repo = IncidentRepository(session_factory)
    starter = FakeTemporalStarter()
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"stuck-key-{uuid.uuid4()}",
        target_id="sim-stuck-target",
        request_sha256="c" * 64,
    )

    # Dispatch 120 seconds in the future (exceeding stuck_age_seconds=60.0)
    future_now = datetime.now(UTC) + timedelta(seconds=120)
    dispatcher = OutboxDispatcher(
        session_factory,
        starter,
        stuck_attempt_threshold=3,
        stuck_age_seconds=60.0,
    )
    await dispatcher.dispatch_once(now=future_now)

    stuck_events = metrics.get_events("outbox.stuck")
    assert any(e.get("incident_id") == str(outcome.response.id) for e in stuck_events)
    snap = metrics.snapshot()
    assert snap["counters"]["outbox_stuck_total"] >= 1
    assert snap["gauges"]["stuck_workflow_age_seconds"] >= 60.0
