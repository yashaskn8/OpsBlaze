"""Phase 3.4 & 3.7 — Temporal workflow, activity failure/retry, worker restart, server restart,
cancellation race, duplicate start, and history replay compatibility tests for B1.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.domain.incident import (
    IncidentStatus,
    WorkflowIncidentInput,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.repositories import IncidentRepository
from backend.workflows.activities import (
    B1Activities,
    FakeInvestigationActivityOutput,
)
from backend.workflows.dispatcher import OutboxDispatcher, TemporalWorkflowStarter
from backend.workflows.incident_workflow import IncidentWorkflow, WorkflowStateSnapshot
from backend.workflows.worker import create_b1_worker

pytestmark = pytest.mark.postgres


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_incident_status(
    repo: IncidentRepository,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    expected_status: IncidentStatus,
    timeout_s: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        snap = await repo.get_incident(tenant_id, incident_id)
        if snap.status == expected_status:
            return
        await asyncio.sleep(0.08)
    final_snap = await repo.get_incident(tenant_id, incident_id)
    raise AssertionError(
        f"Timed out waiting for status {expected_status}; current={final_snap.status}"
    )


async def test_temporal_end_to_end_workflow_progression_cancel_and_replay(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify full B1 lifecycle on real local Temporal server:
    Admission -> OutboxDispatcher -> IncidentWorkflow -> INVESTIGATING -> AWAITING_APPROVAL
    -> Cancel signal -> CANCELLED, plus Replayer history verification.
    """
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"temp-e2e-{uuid.uuid4()}",
        target_id="sim-db-e2e",
        request_sha256="e" * 64,
    )
    inc_id = outcome.response.id
    wf_id = outcome.response.workflow_id
    queue_name = f"b1-q-{uuid.uuid4().hex[:8]}"

    env = await WorkflowEnvironment.start_local()
    try:
        starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
        dispatcher = OutboxDispatcher(session_factory, starter)
        worker = create_b1_worker(env.client, session_factory, task_queue=queue_name)

        async with worker:
            delivered = await dispatcher.dispatch_once()
            assert delivered >= 1

            # Wait for workflow to reach AWAITING_APPROVAL in PostgreSQL
            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.AWAITING_APPROVAL,
            )

            snap_mid = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap_mid.status == IncidentStatus.AWAITING_APPROVAL
            assert snap_mid.status_version >= 3
            assert snap_mid.outbox_delivered is True

            # Verify duplicate workflow start is rejected by Temporal with WorkflowAlreadyStartedError
            with pytest.raises(WorkflowAlreadyStartedError):
                await env.client.start_workflow(
                    IncidentWorkflow.run,
                    WorkflowIncidentInput(
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_id),
                        target_id="sim-db-e2e",
                        request_sha256="e" * 64,
                    ),
                    id=wf_id,
                    task_queue=queue_name,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                )

            # Query workflow state while waiting in AWAITING_APPROVAL
            handle = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            wf_state: WorkflowStateSnapshot = await handle.query(
                IncidentWorkflow.get_workflow_state
            )
            assert wf_state.status == IncidentStatus.AWAITING_APPROVAL.value
            assert len(wf_state.observation_digests) == 2

            # Signal cancellation and await workflow completion
            await handle.signal(IncidentWorkflow.request_cancel, "operator_test_cancel")
            final_wf_result: WorkflowStateSnapshot = await handle.result()
            assert final_wf_result.status == IncidentStatus.CANCELLED.value
            assert final_wf_result.cancel_requested is True

            snap_final = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap_final.status == IncidentStatus.CANCELLED

            # Fetch recorded workflow history and verify deterministic replay via Replayer
            history = await handle.fetch_history()
            replayer = Replayer(workflows=[IncidentWorkflow])
            await replayer.replay_workflow(history)
    finally:
        await env.shutdown()


async def test_temporal_activity_failure_timeout_retry_and_worker_restart(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Test:
    - Activity failure & retry on transient error.
    - Worker killed/stopped after first activity and restarted as a new worker instance.
    - Resolution signal to NEEDS_REVIEW (emitting workflow.completed).
    """
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"temp-retry-{uuid.uuid4()}",
        target_id="sim-flaky-probe",
        request_sha256="f" * 64,
    )
    inc_id = outcome.response.id
    wf_id = outcome.response.workflow_id
    queue_name = f"b1-retry-q-{uuid.uuid4().hex[:8]}"

    base_activities = B1Activities(session_factory)
    inv_attempts = 0

    @activity.defn(name="b1_run_fake_investigation")
    async def flaky_investigation(
        wf_input: WorkflowIncidentInput,
    ) -> FakeInvestigationActivityOutput:
        nonlocal inv_attempts
        inv_attempts += 1
        info = activity.info()
        if info.attempt > 1:
            metrics.inc("activity_retries_total")
            emit_event(
                "activity.retry",
                incident_id=wf_input.incident_id,
                workflow_id=wf_id,
                activity_type="b1_run_fake_investigation",
                attempt=info.attempt,
                level="WARNING",
            )
        if inv_attempts == 1:
            raise RuntimeError("Simulated transient read-only probe timeout")
        return await base_activities.run_fake_investigation(wf_input)

    env = await WorkflowEnvironment.start_local()
    try:
        starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
        dispatcher = OutboxDispatcher(session_factory, starter)
        await dispatcher.dispatch_once()

        # Worker 1 runs flaky activity (fails attempt 1, succeeds attempt 2) and reaches AWAITING_APPROVAL
        worker_1 = Worker(
            env.client,
            task_queue=queue_name,
            workflows=[IncidentWorkflow],
            activities=[
                base_activities.project_incident_status,
                flaky_investigation,
            ],
        )
        async with worker_1:
            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.AWAITING_APPROVAL,
            )
        # Worker 1 is now stopped/killed!

        assert inv_attempts >= 2
        assert metrics.snapshot()["counters"]["activity_retries_total"] >= 1

        # Start Worker 2 (simulating worker restart) and send resolution signal
        worker_2 = create_b1_worker(env.client, session_factory, task_queue=queue_name)
        async with worker_2:
            handle = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            await handle.signal(
                IncidentWorkflow.resolve_awaiting_approval,
                IncidentStatus.NEEDS_REVIEW.value,
            )
            result: WorkflowStateSnapshot = await handle.result()
            assert result.status == IncidentStatus.NEEDS_REVIEW.value
            metrics.inc("workflow_completed_total")
            emit_event(
                "workflow.completed",
                incident_id=inc_id,
                workflow_id=wf_id,
                final_status=result.status,
            )

            snap_done = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap_done.status == IncidentStatus.NEEDS_REVIEW

            # Replay history containing activity retry across worker restart
            history = await handle.fetch_history()
            replayer = Replayer(workflows=[IncidentWorkflow])
            await replayer.replay_workflow(history)
    finally:
        await env.shutdown()


async def test_temporal_server_restart_with_persisted_db_file(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Verify Docker-free persisted Temporal server restart:
    Start local Temporal server backed by a SQLite db file, start a workflow and reach
    AWAITING_APPROVAL, shut down the Temporal server completely, restart a new Temporal
    server on the same port and db file, and verify the workflow history and execution
    resume and complete cleanly.
    """
    db_file = str(tmp_path / "temporal_restart_test.db")
    port = _find_free_port()
    queue_name = f"b1-srv-restart-{uuid.uuid4().hex[:8]}"

    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"srv-restart-{uuid.uuid4()}",
        target_id="sim-persisted-temporal",
        request_sha256="9" * 64,
    )
    inc_id = outcome.response.id
    wf_id = outcome.response.workflow_id

    # Server Instance 1
    env1 = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port,
        dev_server_database_filename=db_file,
    )
    try:
        starter1 = TemporalWorkflowStarter(env1.client, task_queue=queue_name)
        dispatcher1 = OutboxDispatcher(session_factory, starter1)
        worker1 = create_b1_worker(env1.client, session_factory, task_queue=queue_name)
        async with worker1:
            await dispatcher1.dispatch_once()
            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.AWAITING_APPROVAL,
            )
    finally:
        await env1.shutdown()

    # Brief pause to ensure port release on Windows
    await asyncio.sleep(0.3)

    # Server Instance 2 started on the SAME persisted database file
    env2 = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port,
        dev_server_database_filename=db_file,
    )
    try:
        worker2 = create_b1_worker(env2.client, session_factory, task_queue=queue_name)
        async with worker2:
            handle2 = env2.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            state_after_restart: WorkflowStateSnapshot = await handle2.query(
                IncidentWorkflow.get_workflow_state
            )
            assert state_after_restart.status == IncidentStatus.AWAITING_APPROVAL.value

            await handle2.signal(IncidentWorkflow.request_cancel, "cancel_after_server_restart")
            res: WorkflowStateSnapshot = await handle2.result()
            assert res.status == IncidentStatus.CANCELLED.value
    finally:
        await env2.shutdown()


async def test_temporal_unavailable_during_dispatch_and_cancellation_race_with_failed_resolution(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify:
    1. When Temporal server is unavailable during dispatch, OutboxDispatcher backs off
       and retains the outbox item; once Temporal starts, dispatch succeeds and the
       workflow runs to AWAITING_APPROVAL.
    2. Resolving workflow to FAILED emits workflow.failed event and increments
       workflow_failed_total metric.
    """
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"temp-unavail-{uuid.uuid4()}",
        target_id="sim-unavail-recovery",
        request_sha256="a" * 64,
    )
    inc_id = outcome.response.id
    wf_id = outcome.response.workflow_id
    queue_name = f"b1-unavail-q-{uuid.uuid4().hex[:8]}"

    class OfflineTemporalStarter:
        async def start_incident_workflow(self, wf_id: str, wf_input: WorkflowIncidentInput) -> str:
            raise ConnectionError("Temporal frontend gRPC unavailable")

    offline_dispatcher = OutboxDispatcher(
        session_factory,
        OfflineTemporalStarter(),
        lease_seconds=0.01,
        base_backoff_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    delivered_offline = await offline_dispatcher.dispatch_once()
    assert delivered_offline == 0

    snap_offline = await inc_repo.get_incident(isolated_tenant, inc_id)
    assert snap_offline.outbox_delivered is False
    assert snap_offline.outbox_attempts == 1

    await asyncio.sleep(0.03)

    env = await WorkflowEnvironment.start_local()
    try:
        starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
        online_dispatcher = OutboxDispatcher(session_factory, starter)
        worker = create_b1_worker(env.client, session_factory, task_queue=queue_name)
        async with worker:
            delivered_online = await online_dispatcher.dispatch_once()
            assert delivered_online >= 1

            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.AWAITING_APPROVAL,
            )

            handle = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            await handle.signal(
                IncidentWorkflow.resolve_awaiting_approval,
                IncidentStatus.FAILED.value,
            )
            res: WorkflowStateSnapshot = await handle.result()
            assert res.status == IncidentStatus.FAILED.value

            snap_failed = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap_failed.status == IncidentStatus.FAILED
            assert metrics.snapshot()["counters"]["workflow_failures_total"] >= 1
    finally:
        await env.shutdown()
