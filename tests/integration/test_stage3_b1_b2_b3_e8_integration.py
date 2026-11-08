"""Full Stage 3 Integration Tests (B1 + B2 + B3 + E8) & Omega Adversarial Scenarios."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from backend.adapters.conformance import (
    ConformanceRunner,
    ConformanceStatus,
    MutationCapabilityGate,
)
from backend.adapters.fault_proxy import (
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import (
    ApprovedExecutionSignalInput,
    IncidentStatus,
)
from backend.effects.receipts import ActionSpec
from backend.persistence.models import (
    AdapterConformance,
)
from backend.persistence.repositories import IncidentRepository
from backend.workflows.activities import B1_TASK_QUEUE
from backend.workflows.dispatcher import OutboxDispatcher, TemporalWorkflowStarter
from backend.workflows.incident_workflow import IncidentWorkflow
from backend.workflows.worker import create_b1_worker


async def _admit_and_advance_to_awaiting_approval(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    env: WorkflowEnvironment,
    tenant_id: uuid.UUID,
    target_id: str,
) -> tuple[uuid.UUID, str]:
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"stage3-e2e-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="c" * 64,
    )
    inc_id = outcome.response.id
    wf_id = outcome.response.workflow_id

    dispatcher = OutboxDispatcher(
        session_factory=session_factory,
        workflow_starter=TemporalWorkflowStarter(env.client, task_queue=B1_TASK_QUEUE),
    )
    await dispatcher.dispatch_once()

    # Wait until incident reaches AWAITING_APPROVAL in PostgreSQL
    for _ in range(60):
        snap = await inc_repo.get_incident(tenant_id, inc_id)
        if snap.status == IncidentStatus.AWAITING_APPROVAL:
            return inc_id, wf_id
        await asyncio.sleep(0.05)
    raise AssertionError(f"Incident {inc_id} did not reach AWAITING_APPROVAL in time")


async def test_stage3_full_pipeline_happy_path_and_lost_response_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenarios 1 & 2:
    1) B1 durable incident -> Fake investigation -> AWAITING_APPROVAL -> approve_and_execute signal
       -> EXECUTING -> B2 reservation -> E8 verification -> B3 receipt intent -> ActionGateway
       -> DisposableTargetSimulator -> VERIFYING -> IndependentPostconditionVerifier -> VERIFIED.
    2) Same pipeline with LOST_RESPONSE_AFTER_COMMIT -> UNKNOWN -> EffectReconciler -> VERIFIED.
    """
    adapter_id = f"sim-s3-e2e-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    proxy = FaultInjectionProxy(sim)
    runner = ConformanceRunner(session_factory)
    inc_repo = IncidentRepository(session_factory)

    try:
        conf_rep = await runner.run_conformance_suite(sim)
        assert conf_rep.passed is True

        async with await WorkflowEnvironment.start_local() as env:
            worker = create_b1_worker(
                env.client,
                session_factory,
                task_queue=B1_TASK_QUEUE,
                adapters={adapter_id: proxy},
            )
            async with worker:
                # 1. Happy path end-to-end
                target_1 = "sim-e2e-target-1"
                inc_1, wf_1 = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=target_1,
                )
                act_1 = ActionSpec(action_kind="restart_service", parameters={"graceful": True})
                now = datetime.now(UTC)
                handle_1 = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_1)
                await handle_1.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_1),
                        target_id=target_1,
                        adapter_id=adapter_id,
                        plan_digest="plan-e2e-1",
                        action_kind=act_1.action_kind,
                        action_digest=act_1.action_digest(),
                        parameters=act_1.parameters,
                        expected_version="v1",
                        approved_by_role="incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                wf_out_1 = await asyncio.wait_for(handle_1.result(), timeout=15.0)
                assert wf_out_1.status == IncidentStatus.VERIFIED.value

                inc_snap_1 = await inc_repo.get_incident(isolated_tenant, inc_1)
                assert inc_snap_1.status == IncidentStatus.VERIFIED
                res_snap_1 = await sim.read_resource(isolated_tenant, target_1)
                assert res_snap_1.current_version == "v2"
                assert res_snap_1.highest_fence_seen == 1

                # 2. Lost response after commit -> UNKNOWN -> reconciled to VERIFIED
                target_2 = "sim-e2e-target-2"
                inc_2, wf_2 = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=target_2,
                )
                proxy.set_rule(
                    FaultRule(
                        scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
                        target_id=target_2,
                        max_triggers=1,
                    )
                )
                act_2 = ActionSpec(action_kind="drain_traffic")
                handle_2 = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_2)
                await handle_2.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_2),
                        target_id=target_2,
                        adapter_id=adapter_id,
                        plan_digest="plan-e2e-2",
                        action_kind=act_2.action_kind,
                        action_digest=act_2.action_digest(),
                        parameters=act_2.parameters,
                        expected_version="v1",
                        approved_by_role="incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                wf_out_2 = await asyncio.wait_for(handle_2.result(), timeout=15.0)
                assert wf_out_2.status == IncidentStatus.VERIFIED.value

                inc_snap_2 = await inc_repo.get_incident(isolated_tenant, inc_2)
                assert inc_snap_2.status == IncidentStatus.VERIFIED
                res_snap_2 = await sim.read_resource(isolated_tenant, target_2)
                assert res_snap_2.current_version == "v2"
                assert res_snap_2.state_data["mutation_count"] == 1
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()


async def test_stage3_failure_modes_version_conflict_partial_false_success_and_unapproved(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenarios 5, 6, 7, 8, 9:
    - Target version conflict before dispatch -> FAILED.
    - Partial effect -> NEEDS_REVIEW.
    - False success response -> NEEDS_REVIEW + adapter QUARANTINED.
    - Unapproved / model-self-approved action -> rejected -> FAILED with zero adapter apply calls.
    """
    adapter_id = f"sim-s3-failures-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    proxy = FaultInjectionProxy(sim)
    runner = ConformanceRunner(session_factory)
    gate = MutationCapabilityGate(session_factory)
    inc_repo = IncidentRepository(session_factory)

    try:
        await runner.run_conformance_suite(sim)
        now = datetime.now(UTC)

        async with await WorkflowEnvironment.start_local() as env:
            worker = create_b1_worker(
                env.client,
                session_factory,
                task_queue=B1_TASK_QUEUE,
                adapters={adapter_id: proxy},
            )
            async with worker:
                # A. Unapproved / agent self-approval attempt -> FAILED with zero mutations
                t_unapp = "sim-e2e-unapproved"
                inc_u, wf_u = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=t_unapp,
                )
                act_u = ActionSpec(action_kind="restart_service")
                handle_u = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_u)
                await handle_u.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_u),
                        target_id=t_unapp,
                        adapter_id=adapter_id,
                        plan_digest="plan-unapp",
                        action_kind=act_u.action_kind,
                        action_digest=act_u.action_digest(),
                        parameters={},
                        expected_version="v1",
                        approved_by_role="agent",  # Forbidden role!
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                out_u = await asyncio.wait_for(handle_u.result(), timeout=15.0)
                assert out_u.status == IncidentStatus.FAILED.value
                snap_u = await sim.read_resource(isolated_tenant, t_unapp)
                assert snap_u.current_version == "v1"
                assert snap_u.state_data["mutation_count"] == 0

                # B. Target version conflict -> FAILED
                t_vc = "sim-e2e-version-conflict"
                inc_vc, wf_vc = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=t_vc,
                )
                sim.advance_version_out_of_band(isolated_tenant, t_vc)
                handle_vc = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_vc)
                await handle_vc.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_vc),
                        target_id=t_vc,
                        adapter_id=adapter_id,
                        plan_digest="plan-vc",
                        action_kind="restart_service",
                        action_digest=act_u.action_digest(),
                        parameters={},
                        expected_version="v1",  # Stale version!
                        approved_by_role="incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                out_vc = await asyncio.wait_for(handle_vc.result(), timeout=15.0)
                assert out_vc.status == IncidentStatus.FAILED.value

                # C. Partial effect -> NEEDS_REVIEW
                t_part = "sim-e2e-partial"
                inc_pt, wf_pt = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=t_part,
                )
                proxy.clear_rules()
                proxy.set_rule(
                    FaultRule(
                        scenario=FaultScenario.PARTIAL_EFFECT,
                        target_id=t_part,
                        max_triggers=1,
                    )
                )
                handle_pt = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_pt)
                await handle_pt.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_pt),
                        target_id=t_part,
                        adapter_id=adapter_id,
                        plan_digest="plan-pt",
                        action_kind="restart_service",
                        action_digest=act_u.action_digest(),
                        parameters={},
                        expected_version="v1",
                        approved_by_role="incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                out_pt = await asyncio.wait_for(handle_pt.result(), timeout=15.0)
                assert out_pt.status == IncidentStatus.NEEDS_REVIEW.value

                # D. False success response -> NEEDS_REVIEW + adapter QUARANTINED
                t_fs = "sim-e2e-false-success"
                inc_fs, wf_fs = await _admit_and_advance_to_awaiting_approval(
                    session_factory=session_factory,
                    env=env,
                    tenant_id=isolated_tenant,
                    target_id=t_fs,
                )
                proxy.clear_rules()
                proxy.set_rule(
                    FaultRule(
                        scenario=FaultScenario.FALSE_SUCCESS_RESPONSE,
                        target_id=t_fs,
                        max_triggers=1,
                    )
                )
                handle_fs = env.client.get_workflow_handle_for(IncidentWorkflow.run, wf_fs)
                await handle_fs.signal(
                    IncidentWorkflow.approve_and_execute,
                    ApprovedExecutionSignalInput(
                        approval_id=str(uuid.uuid4()),
                        tenant_id=str(isolated_tenant),
                        incident_id=str(inc_fs),
                        target_id=t_fs,
                        adapter_id=adapter_id,
                        plan_digest="plan-fs",
                        action_kind="restart_service",
                        action_digest=act_u.action_digest(),
                        parameters={},
                        expected_version="v1",
                        approved_by_role="incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        expires_at_iso=(now + timedelta(minutes=15)).isoformat(),
                    ),
                )
                out_fs = await asyncio.wait_for(handle_fs.result(), timeout=15.0)
                assert out_fs.status == IncidentStatus.NEEDS_REVIEW.value
                inc_snap_fs = await inc_repo.get_incident(isolated_tenant, inc_fs)
                assert inc_snap_fs.status == IncidentStatus.NEEDS_REVIEW

                # Adapter is now QUARANTINED; any subsequent attempt fails closed
                conf_after = await gate.get_conformance_record(adapter_id)
                assert conf_after is not None
                assert conf_after.status == ConformanceStatus.QUARANTINED
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()
