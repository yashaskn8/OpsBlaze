"""B1 restart recovery with a signal durably stored while no worker exists."""

import asyncio
from collections import Counter
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_restart_replays_approval_signal_without_duplicate_actions():
    reached_approval = asyncio.Event()
    calls: Counter[str] = Counter()
    states: list[str] = []

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
        return "VERIFIED"

    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-worker-restart-{uuid4()}"
        worker_config = dict(
            client=env.client,
            task_queue=queue,
            workflows=[IncidentWorkflow],
            activities=[project, route, inspect, apply],
        )

        # Only the first worker performs the initial read-only activities.
        async with Worker(**worker_config):
            handle = await env.client.start_workflow(
                IncidentWorkflow.run,
                args=["tenant", str(uuid4())],
                id=f"restart-{uuid4()}", task_queue=queue,
            )
            await asyncio.wait_for(reached_approval.wait(), timeout=15)

        assert calls["route"] == 1
        assert calls["inspect"] == 1
        assert calls["apply"] == 0

        # The workflow task remains in Temporal. No worker is connected when
        # the signal arrives, so recovery tests server durability.
        await handle.signal(
            IncidentWorkflow.plan_decided, args=["fixture-plan", True]
        )
        async with Worker(**worker_config):
            assert await asyncio.wait_for(handle.result(), timeout=15) == "VERIFIED"

        assert states.count("INVESTIGATING") == 1
        assert states.count("AWAITING_APPROVAL") == 1
        assert states[-1] == "VERIFIED"
        assert calls["route"] == 1
        assert calls["inspect"] == 1
        assert calls["apply"] == 1
