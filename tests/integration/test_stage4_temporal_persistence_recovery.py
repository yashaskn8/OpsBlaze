"""Stage 4 Phase 3 — Temporal Persistence Recovery & SQLite Online Backup Tests.

Verifies:
1. Real persisted Temporal state across graceful stop and restart of workers and Temporal dev server
   backed by an isolated temporary SQLite database file (never touching `.local/temporal.db`).
2. Online SQLite backup using Python's `sqlite3.connect(src).backup(sqlite3.connect(dst))`
   without corrupting active WAL/journal transactions.
3. Restoring Temporal server from the SQLite backup file and resuming in-flight workflow
   executions without non-determinism or activity re-execution.
4. Stage 3 approved execution (`approve_and_execute`) completing through to `VERIFIED`
   across server stop and restart on the persisted database.
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import (
    ApprovedExecutionSignalInput,
    IncidentStatus,
)
from backend.effects.receipts import ActionSpec
from backend.persistence.repositories import IncidentRepository
from backend.workflows.activities import B1Activities
from backend.workflows.dispatcher import OutboxDispatcher, TemporalWorkflowStarter
from backend.workflows.incident_workflow import IncidentWorkflow, WorkflowStateSnapshot


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_status(
    repo: IncidentRepository,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    expected_status: IncidentStatus,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        snap = await repo.get_incident(tenant_id, incident_id)
        if snap.status == expected_status:
            return
        await asyncio.sleep(0.05)
    snap = await repo.get_incident(tenant_id, incident_id)
    assert snap.status == expected_status, (
        f"Timeout waiting for {expected_status}, current: {snap.status}"
    )


async def test_1_temporal_sqlite_online_backup_and_restart_recovery(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Verify:
    1. Start Temporal server backed by `db_primary.db`.
    2. Start workflow and progress to AWAITING_APPROVAL.
    3. Perform online SQLite backup: `db_primary.db -> db_backup.db` using sqlite3 backup API.
    4. Shut down primary server.
    5. Start new Temporal server instance pointing to `db_backup.db`.
    6. Verify workflow query returns AWAITING_APPROVAL.
    7. Signal workflow to resolve to NEEDS_REVIEW and verify completion and PG projection.
    """
    db_primary = str(tmp_path / "temporal_primary.db")
    db_backup = str(tmp_path / "temporal_backup.db")
    port = _find_free_port()
    queue_name = f"s4-temp-rec-{uuid.uuid4().hex[:8]}"

    inc_repo = IncidentRepository(session_factory)
    out = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"s4-temp-bk-{uuid.uuid4()}",
        target_id="sim-temp-backup-target",
        request_sha256="4" * 64,
    )
    inc_id = out.response.id
    wf_id = out.response.workflow_id

    # 1. Primary Server Instance
    env_primary = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port,
        dev_server_database_filename=db_primary,
    )
    try:
        starter1 = TemporalWorkflowStarter(env_primary.client, task_queue=queue_name)
        dispatcher1 = OutboxDispatcher(session_factory, starter1)
        act1 = B1Activities(session_factory)
        worker1 = Worker(
            env_primary.client,
            task_queue=queue_name,
            workflows=[IncidentWorkflow],
            activities=[act1.project_incident_status, act1.run_fake_investigation],
        )
        async with worker1:
            await dispatcher1.dispatch_once()
            await _wait_for_status(
                inc_repo, isolated_tenant, inc_id, IncidentStatus.AWAITING_APPROVAL
            )

        # 2. Perform online SQLite backup using Python's sqlite3 backup API
        def _sqlite_backup(src_path: str, dst_path: str) -> None:
            src = sqlite3.connect(src_path)
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

        await asyncio.to_thread(_sqlite_backup, db_primary, db_backup)
        assert Path(db_backup).exists()
        assert Path(db_backup).stat().st_size > 0
    finally:
        await env_primary.shutdown()

    await asyncio.sleep(0.3)

    # 3. Restore on new server instance backed ONLY by `db_backup.db`
    port2 = _find_free_port()
    env_restored = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port2,
        dev_server_database_filename=db_backup,
    )
    try:
        act2 = B1Activities(session_factory)
        worker2 = Worker(
            env_restored.client,
            task_queue=queue_name,
            workflows=[IncidentWorkflow],
            activities=[act2.project_incident_status, act2.run_fake_investigation],
        )
        async with worker2:
            handle: WorkflowHandle[IncidentWorkflow, WorkflowStateSnapshot] = (
                env_restored.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            )
            state: WorkflowStateSnapshot = await handle.query(IncidentWorkflow.get_workflow_state)
            assert state.status == IncidentStatus.AWAITING_APPROVAL.value

            # Resolve awaiting approval to NEEDS_REVIEW
            await handle.signal(
                IncidentWorkflow.resolve_awaiting_approval,
                IncidentStatus.NEEDS_REVIEW.value,
            )
            res = await handle.result()
            assert res.status == IncidentStatus.NEEDS_REVIEW.value

            snap_final = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap_final.status == IncidentStatus.NEEDS_REVIEW
    finally:
        await env_restored.shutdown()


async def test_2_stage3_approved_execution_across_temporal_server_restart(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Verify Stage 3 approved simulator repair execution across Temporal server restart:
    1. Start Temporal server with persisted SQLite database.
    2. Progress to AWAITING_APPROVAL.
    3. Shut down Temporal server.
    4. Restart Temporal server with the same SQLite database.
    5. Register Stage 3 activities and adapter conformance.
    6. Signal `approve_and_execute` and verify workflow completes through to `VERIFIED`.
    """
    db_file = str(tmp_path / "temporal_stage3_restart.db")
    port = _find_free_port()
    queue_name = f"s4-temp-st3-{uuid.uuid4().hex[:8]}"
    adapter_id = f"sim-s4-temp-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    now = datetime.now(UTC)

    runner = ConformanceRunner(session_factory)
    await runner.run_conformance_suite(sim, now=now)

    inc_repo = IncidentRepository(session_factory)
    out = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"s4-st3-restart-{uuid.uuid4()}",
        target_id="sim-s4-target",
        request_sha256="5" * 64,
    )
    inc_id = out.response.id
    wf_id = out.response.workflow_id

    # Server 1: Progress to AWAITING_APPROVAL
    env1 = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port,
        dev_server_database_filename=db_file,
    )
    try:
        starter1 = TemporalWorkflowStarter(env1.client, task_queue=queue_name)
        dispatcher1 = OutboxDispatcher(session_factory, starter1)
        act1 = B1Activities(session_factory)
        worker1 = Worker(
            env1.client,
            task_queue=queue_name,
            workflows=[IncidentWorkflow],
            activities=[act1.project_incident_status, act1.run_fake_investigation],
        )
        async with worker1:
            await dispatcher1.dispatch_once()
            await _wait_for_status(
                inc_repo, isolated_tenant, inc_id, IncidentStatus.AWAITING_APPROVAL
            )
    finally:
        await env1.shutdown()

    await asyncio.sleep(0.3)

    # Server 2: Restart on same database, submit Stage 3 approved execution signal
    env2 = await WorkflowEnvironment.start_local(
        ip="127.0.0.1",
        port=port,
        dev_server_database_filename=db_file,
    )
    try:
        act2 = B1Activities(
            session_factory,
            adapters={adapter_id: sim},
        )
        worker2 = Worker(
            env2.client,
            task_queue=queue_name,
            workflows=[IncidentWorkflow],
            activities=[
                act2.project_incident_status,
                act2.run_fake_investigation,
                act2.execute_approved_action,
                act2.verify_and_finalize_effect,
            ],
        )
        async with worker2:
            handle2: WorkflowHandle[IncidentWorkflow, WorkflowStateSnapshot] = (
                env2.client.get_workflow_handle_for(IncidentWorkflow.run, wf_id)
            )
            state2 = await handle2.query(IncidentWorkflow.get_workflow_state)
            assert state2.status == IncidentStatus.AWAITING_APPROVAL.value

            act_spec = ActionSpec(action_kind="restart_service", parameters={"mode": "recovery"})
            exec_sig = ApprovedExecutionSignalInput(
                approval_id=str(uuid.uuid4()),
                tenant_id=str(isolated_tenant),
                incident_id=str(inc_id),
                target_id="sim-s4-target",
                adapter_id=adapter_id,
                plan_digest="plan-s4-restart-1",
                action_kind=act_spec.action_kind,
                action_digest=act_spec.action_digest(),
                expected_version="v1",
                parameters=act_spec.parameters,
                expires_at_iso=(now + timedelta(minutes=30)).isoformat(),
            )
            await handle2.signal(IncidentWorkflow.approve_and_execute, exec_sig)
            res2 = await handle2.result()
            assert res2.status == IncidentStatus.VERIFIED.value

            snap2 = await inc_repo.get_incident(isolated_tenant, inc_id)
            assert snap2.status == IncidentStatus.VERIFIED
    finally:
        await env2.shutdown()
