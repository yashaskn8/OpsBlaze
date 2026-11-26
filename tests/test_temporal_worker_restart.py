"""B1 restart recovery with a signal durably stored while no worker exists."""

import asyncio
import importlib
import os
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.incident import IncidentWorkflow


@pytest.fixture
def starting_workflow(tmp_path, monkeypatch):
    """Use the pinned source checkout in the upgrade run, current code otherwise."""
    baseline = os.environ.get("OPSBLAZE_RESTART_BASELINE")
    if baseline is None:
        return IncidentWorkflow
    source = Path(baseline) / "backend/workflows/incident.py"
    # Import from a separate module so both workflow definitions can coexist.
    # Temporal's sandbox must also be able to import this exact module.
    module_name = "opsblaze_pinned_baseline_workflow"
    (tmp_path / f"{module_name}.py").write_bytes(source.read_bytes())
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    assert Path(module.__file__).parent == tmp_path
    assert module.IncidentWorkflow is not IncidentWorkflow
    return module.IncidentWorkflow


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("approve", "VERIFIED"), ("reject", "NEEDS_REVIEW"), ("cancel", "CANCELLED"),
])
async def test_worker_restart_replays_approval_signal_without_duplicate_actions(
    starting_workflow, case, expected,
):
    reached_approval = asyncio.Event()
    calls: Counter[str] = Counter()
    states: list[str] = []
    applied_plans: list[str] = []

    @activity.defn(name="project_incident_status")
    async def project(tenant_id: str, incident_id: str, state: str) -> None:
        states.append(state)
        if state == "AWAITING_APPROVAL":
            reached_approval.set()

    @activity.defn(name="select_incident_route")
    async def route(tenant_id: str, incident_id: str) -> str:
        calls["route"] += 1
        return "RUNBOOK"

    @activity.defn(name="inspect_incident_read_only")
    async def inspect(tenant_id: str, incident_id: str) -> str:
        calls["inspect"] += 1
        return "SIMULATOR_REVIEW_REQUIRED"

    @activity.defn(name="apply_approved_simulator_plan")
    async def apply(tenant_id: str, incident_id: str, plan_id: str) -> str:
        calls["apply"] += 1
        applied_plans.append(plan_id)
        return "VERIFIED"

    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-worker-restart-{uuid4()}"
        worker_config = dict(
            client=env.client,
            task_queue=queue,
            activities=[project, route, inspect, apply],
        )

        # Only the first worker performs the initial read-only activities.
        async with Worker(**worker_config, workflows=[starting_workflow]):
            handle = await env.client.start_workflow(
                starting_workflow.run,
                args=["tenant", str(uuid4())],
                id=f"restart-{uuid4()}", task_queue=queue,
            )
            await asyncio.wait_for(reached_approval.wait(), timeout=15)
            # Wait until the projection activity and following workflow task
            # are durably completed before shutting down the old worker.
            async with asyncio.timeout(15):
                while True:
                    history = await handle.fetch_history()
                    if history.events[-1].HasField("workflow_task_completed_event_attributes"):
                        break
                    await asyncio.sleep(0.05)
            original_run = (await handle.describe()).run_id

        assert calls["route"] == 1
        assert calls["inspect"] == 1
        assert calls["apply"] == 0

        # The workflow task remains in Temporal. No worker is connected when
        # the signal arrives, so recovery tests server durability.
        if case == "cancel":
            await handle.signal(IncidentWorkflow.request_cancel)
        else:
            approved = case == "approve"
            await handle.signal(IncidentWorkflow.plan_decided, args=["fixture-plan", approved])
            # A duplicate or conflicting delivery must not change the first
            # durable decision, including a rejection followed by approval.
            await handle.signal(IncidentWorkflow.plan_decided, args=["other-plan", not approved])
        async with Worker(**worker_config, workflows=[IncidentWorkflow]):
            assert await asyncio.wait_for(handle.result(), timeout=15) == expected
        assert (await handle.describe()).run_id == original_run

        assert states.count("INVESTIGATING") == 1
        assert states.count("AWAITING_APPROVAL") == 1
        assert states[-1] == expected
        assert calls["route"] == 1
        assert calls["inspect"] == 1
        assert calls["apply"] == int(case == "approve")
        assert applied_plans == (["fixture-plan"] if case == "approve" else [])


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_phase", ["INVESTIGATING", "EXECUTING"])
async def test_worker_restart_during_investigating_and_executing_states(
    interrupt_phase,
):
    entered_phase = asyncio.Event()
    resume_worker_two = False
    calls: Counter[str] = Counter()
    states: list[str] = []

    @activity.defn(name="project_incident_status")
    async def project(tenant_id: str, incident_id: str, state: str) -> None:
        states.append(state)

    @activity.defn(name="select_incident_route")
    async def route(tenant_id: str, incident_id: str) -> str:
        calls["route"] += 1
        return "RUNBOOK"

    @activity.defn(name="inspect_incident_read_only")
    async def inspect(tenant_id: str, incident_id: str) -> str:
        calls["inspect"] += 1
        if interrupt_phase == "INVESTIGATING" and not resume_worker_two:
            entered_phase.set()
            raise RuntimeError("simulated worker interruption during investigating")
        return "SIMULATOR_REVIEW_REQUIRED"

    @activity.defn(name="apply_approved_simulator_plan")
    async def apply(tenant_id: str, incident_id: str, plan_id: str) -> str:
        calls["apply"] += 1
        if interrupt_phase == "EXECUTING" and not resume_worker_two:
            entered_phase.set()
            raise RuntimeError("simulated worker interruption during executing")
        return "VERIFIED"

    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-phase-restart-{uuid4()}"
        worker_config = dict(
            client=env.client,
            task_queue=queue,
            activities=[project, route, inspect, apply],
            workflows=[IncidentWorkflow],
            graceful_shutdown_timeout=timedelta(milliseconds=100),
        )
        async with Worker(**worker_config):
            handle = await env.client.start_workflow(
                IncidentWorkflow.run,
                args=["tenant", str(uuid4())],
                id=f"phase-restart-{uuid4()}",
                task_queue=queue,
            )
            if interrupt_phase == "EXECUTING":
                await asyncio.sleep(0.3)
                await handle.signal(IncidentWorkflow.plan_decided, args=["plan-1", True])
            await asyncio.wait_for(entered_phase.wait(), timeout=15)
            original_run = (await handle.describe()).run_id

        resume_worker_two = True
        async with Worker(**worker_config):
            if interrupt_phase == "INVESTIGATING":
                await handle.signal(IncidentWorkflow.plan_decided, args=["plan-1", True])
            assert await asyncio.wait_for(handle.result(), timeout=15) == "VERIFIED"
        assert (await handle.describe()).run_id == original_run
        assert calls["route"] == 1
        if interrupt_phase == "EXECUTING":
            assert calls["inspect"] == 1
        assert states[-1] == "VERIFIED"
