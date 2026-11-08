"""Script to generate and record the 6 canonical Temporal workflow histories for OpsBlaze Stage 4.

Saves verified JSON histories to `fixtures/workflow_histories/`:
1. `b1_workflow_v1.json`: B1 workflow resolved via resolve_awaiting_approval signal.
2. `stage3_execution_v1.json`: Stage 3 approved execution through to VERIFIED.
3. `awaiting_approval_in_flight.json`: Workflow waiting at AWAITING_APPROVAL human boundary.
4. `cancelled_workflow.json`: Workflow cancelled via request_cancel signal.
5. `activity_retry_workflow.json`: Workflow with activity transient failure and automatic retry.
6. `incomplete_crash_window.json`: Workflow history interrupted mid-execution.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.protocol import TargetAdapterProtocol
from backend.adapters.simulator import DisposableTargetSimulator
from backend.config import get_settings
from backend.domain.incident import (
    ApprovedExecutionSignalInput,
    WorkflowIncidentInput,
)
from backend.effects.receipts import ActionSpec
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import Tenant
from backend.persistence.repositories import IncidentRepository
from backend.workflows.activities import B1Activities, FakeInvestigationActivityOutput
from backend.workflows.incident_workflow import IncidentWorkflow

FIXTURES_DIR = Path("fixtures/workflow_histories")


async def generate_all() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    inc_repo = IncidentRepository(session_factory)

    tenant_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=tenant_id, name=f"hist-gen-{uuid.uuid4().hex[:8]}"))
        await session.commit()

    adapter_id = f"sim-hist-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    runner = ConformanceRunner(session_factory)
    await runner.run_conformance_suite(sim)

    adapters: dict[str, TargetAdapterProtocol] = {adapter_id: sim}
    activities = B1Activities(session_factory, adapters=adapters)

    # -------------------------------------------------------------------------
    # 1. b1_workflow_v1.json
    # -------------------------------------------------------------------------
    print("Generating 1. b1_workflow_v1...")
    out1 = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"hist-1-{uuid.uuid4()}",
        target_id="target-hist-1",
        request_sha256="1" * 64,
    )
    inc1_id = out1.response.id
    tq1 = f"tq-hist1-{uuid.uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq1,
            workflows=[IncidentWorkflow],
            activities=[
                activities.project_incident_status,
                activities.run_fake_investigation,
                activities.execute_approved_action,
                activities.verify_and_finalize_effect,
            ],
        ),
    ):
        handle1 = await env.client.start_workflow(
            IncidentWorkflow.run,
            WorkflowIncidentInput(
                incident_id=str(inc1_id),
                tenant_id=str(tenant_id),
                correlation_id=f"corr-hist-1-{uuid.uuid4()}",
                target_id="target-hist-1",
                request_sha256="1" * 64,
            ),
            id=f"wf-hist-1-{inc1_id}",
            task_queue=tq1,
        )
        while True:
            snap = await handle1.query(IncidentWorkflow.get_workflow_state)
            if snap.status == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.05)

        await handle1.signal(IncidentWorkflow.resolve_awaiting_approval, "NEEDS_REVIEW")
        await handle1.result()
        hist1 = await handle1.fetch_history()
        (FIXTURES_DIR / "b1_workflow_v1.json").write_text(hist1.to_json(), encoding="utf-8")
        print("  Wrote b1_workflow_v1.json:", len(hist1.events), "events")

    # -------------------------------------------------------------------------
    # 2. stage3_execution_v1.json
    # -------------------------------------------------------------------------
    print("Generating 2. stage3_execution_v1...")
    out2 = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"hist-2-{uuid.uuid4()}",
        target_id=adapter_id,
        request_sha256="2" * 64,
    )
    inc2_id = out2.response.id
    tq2 = f"tq-hist2-{uuid.uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq2,
            workflows=[IncidentWorkflow],
            activities=[
                activities.project_incident_status,
                activities.run_fake_investigation,
                activities.execute_approved_action,
                activities.verify_and_finalize_effect,
            ],
        ),
    ):
        handle2 = await env.client.start_workflow(
            IncidentWorkflow.run,
            WorkflowIncidentInput(
                incident_id=str(inc2_id),
                tenant_id=str(tenant_id),
                correlation_id=f"corr-hist-2-{uuid.uuid4()}",
                target_id=adapter_id,
                request_sha256="2" * 64,
            ),
            id=f"wf-hist-2-{inc2_id}",
            task_queue=tq2,
        )
        while True:
            snap = await handle2.query(IncidentWorkflow.get_workflow_state)
            if snap.status == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.05)

        action = ActionSpec(
            action_kind="restart_service",
            parameters={"mode": "recovery"},
        )
        now = datetime.now(UTC)
        sig_input = ApprovedExecutionSignalInput(
            approval_id=str(uuid.uuid4()),
            tenant_id=str(tenant_id),
            incident_id=str(inc2_id),
            target_id=adapter_id,
            adapter_id=adapter_id,
            plan_digest=f"plan-hist2-{uuid.uuid4().hex[:8]}",
            action_kind=action.action_kind,
            action_digest=action.action_digest(),
            expected_version="v1",
            parameters=action.parameters,
            expires_at_iso=(now + timedelta(minutes=30)).isoformat(),
        )
        await handle2.signal(IncidentWorkflow.approve_and_execute, sig_input)
        await handle2.result()
        hist2 = await handle2.fetch_history()
        (FIXTURES_DIR / "stage3_execution_v1.json").write_text(hist2.to_json(), encoding="utf-8")
        print("  Wrote stage3_execution_v1.json:", len(hist2.events), "events")

    # -------------------------------------------------------------------------
    # 3. awaiting_approval_in_flight.json
    # -------------------------------------------------------------------------
    print("Generating 3. awaiting_approval_in_flight...")
    out3 = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"hist-3-{uuid.uuid4()}",
        target_id="target-hist-3",
        request_sha256="3" * 64,
    )
    inc3_id = out3.response.id
    tq3 = f"tq-hist3-{uuid.uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq3,
            workflows=[IncidentWorkflow],
            activities=[
                activities.project_incident_status,
                activities.run_fake_investigation,
                activities.execute_approved_action,
                activities.verify_and_finalize_effect,
            ],
        ),
    ):
        handle3 = await env.client.start_workflow(
            IncidentWorkflow.run,
            WorkflowIncidentInput(
                incident_id=str(inc3_id),
                tenant_id=str(tenant_id),
                correlation_id=f"corr-hist-3-{uuid.uuid4()}",
                target_id="target-hist-3",
                request_sha256="3" * 64,
            ),
            id=f"wf-hist-3-{inc3_id}",
            task_queue=tq3,
        )
        while True:
            snap = await handle3.query(IncidentWorkflow.get_workflow_state)
            if snap.status == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.05)

        hist3 = await handle3.fetch_history()
        (FIXTURES_DIR / "awaiting_approval_in_flight.json").write_text(
            hist3.to_json(), encoding="utf-8"
        )
        print("  Wrote awaiting_approval_in_flight.json:", len(hist3.events), "events")

    # -------------------------------------------------------------------------
    # 4. cancelled_workflow.json
    # -------------------------------------------------------------------------
    print("Generating 4. cancelled_workflow...")
    out4 = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"hist-4-{uuid.uuid4()}",
        target_id="target-hist-4",
        request_sha256="4" * 64,
    )
    inc4_id = out4.response.id
    tq4 = f"tq-hist4-{uuid.uuid4().hex[:8]}"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq4,
            workflows=[IncidentWorkflow],
            activities=[
                activities.project_incident_status,
                activities.run_fake_investigation,
                activities.execute_approved_action,
                activities.verify_and_finalize_effect,
            ],
        ),
    ):
        handle4 = await env.client.start_workflow(
            IncidentWorkflow.run,
            WorkflowIncidentInput(
                incident_id=str(inc4_id),
                tenant_id=str(tenant_id),
                correlation_id=f"corr-hist-4-{uuid.uuid4()}",
                target_id="target-hist-4",
                request_sha256="4" * 64,
            ),
            id=f"wf-hist-4-{inc4_id}",
            task_queue=tq4,
        )
        while True:
            snap = await handle4.query(IncidentWorkflow.get_workflow_state)
            if snap.status == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.05)

        await handle4.signal(IncidentWorkflow.request_cancel, "operator_manual_cancellation")
        await handle4.result()
        hist4 = await handle4.fetch_history()
        (FIXTURES_DIR / "cancelled_workflow.json").write_text(hist4.to_json(), encoding="utf-8")
        print("  Wrote cancelled_workflow.json:", len(hist4.events), "events")

    # -------------------------------------------------------------------------
    # 5. activity_retry_workflow.json
    # -------------------------------------------------------------------------
    print("Generating 5. activity_retry_workflow...")
    out5 = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"hist-5-{uuid.uuid4()}",
        target_id="target-hist-5",
        request_sha256="5" * 64,
    )
    inc5_id = out5.response.id
    tq5 = f"tq-hist5-{uuid.uuid4().hex[:8]}"

    investigation_attempts = 0

    @activity.defn(name="b1_run_fake_investigation")
    async def flaky_investigation(
        wf_input: WorkflowIncidentInput,
    ) -> FakeInvestigationActivityOutput:
        nonlocal investigation_attempts
        investigation_attempts += 1
        if investigation_attempts == 1:
            raise ApplicationError("Simulated transient connection timeout", non_retryable=False)
        return FakeInvestigationActivityOutput(
            incident_id=wf_input.incident_id,
            tenant_id=wf_input.tenant_id,
            target_id=wf_input.target_id,
            hypothesis="flaky probe resolved on attempt 2",
            recommended_status="AWAITING_APPROVAL",
            observation_count=1,
            observation_digests=["retry-digest-1"],
        )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq5,
            workflows=[IncidentWorkflow],
            activities=[
                activities.project_incident_status,
                flaky_investigation,
                activities.execute_approved_action,
                activities.verify_and_finalize_effect,
            ],
        ),
    ):
        handle5 = await env.client.start_workflow(
            IncidentWorkflow.run,
            WorkflowIncidentInput(
                incident_id=str(inc5_id),
                tenant_id=str(tenant_id),
                correlation_id=f"corr-hist-5-{uuid.uuid4()}",
                target_id="target-hist-5",
                request_sha256="5" * 64,
            ),
            id=f"wf-hist-5-{inc5_id}",
            task_queue=tq5,
        )
        while True:
            snap = await handle5.query(IncidentWorkflow.get_workflow_state)
            if snap.status == "AWAITING_APPROVAL":
                break
            await asyncio.sleep(0.05)

        await handle5.signal(IncidentWorkflow.resolve_awaiting_approval, "NEEDS_REVIEW")
        await handle5.result()
        hist5 = await handle5.fetch_history()
        (FIXTURES_DIR / "activity_retry_workflow.json").write_text(
            hist5.to_json(), encoding="utf-8"
        )
        print("  Wrote activity_retry_workflow.json:", len(hist5.events), "events")

    # -------------------------------------------------------------------------
    # 6. incomplete_crash_window.json
    # -------------------------------------------------------------------------
    print("Generating 6. incomplete_crash_window...")
    # An incomplete workflow history represents a workflow that stopped midway (e.g. at INVESTIGATING)
    # when the host machine crashed or crashed before approval.
    # We take the first 15 events of hist1 (which cuts it after INVESTIGATING activity completion).
    events_cut = list(hist1.events)[:15]
    from temporalio.client import WorkflowHistory

    hist6 = WorkflowHistory(workflow_id=f"wf-hist-6-{inc1_id}", events=events_cut)
    (FIXTURES_DIR / "incomplete_crash_window.json").write_text(hist6.to_json(), encoding="utf-8")
    print("  Wrote incomplete_crash_window.json:", len(events_cut), "events")

    await engine.dispose()
    print("All 6 workflow histories generated successfully!")


if __name__ == "__main__":
    asyncio.run(generate_all())
