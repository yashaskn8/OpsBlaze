"""B1 durable human plan decisions survive duplicate/conflicting signals."""

from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporal_first_plan_decision_wins_even_without_a_worker():
    applied: list[str] = []

    @activity.defn(name="project_incident_status")
    async def project(tenant_id: str, incident_id: str, state: str) -> None:
        return None

    @activity.defn(name="select_incident_route")
    async def route(tenant_id: str, incident_id: str) -> str:
        return "RUNBOOK"

    @activity.defn(name="inspect_incident_read_only")
    async def inspect(tenant_id: str, incident_id: str) -> str:
        return "SIMULATOR_REVIEW_REQUIRED"

    @activity.defn(name="apply_approved_simulator_plan")
    async def apply(tenant_id: str, incident_id: str, plan_id: str) -> str:
        applied.append(plan_id)
        return "VERIFIED"

    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-first-decision-{uuid4()}"
        # Signal history is accepted while the queue has no running worker.
        approved = await env.client.start_workflow(
            IncidentWorkflow.run, args=["tenant", str(uuid4())],
            id=f"approved-{uuid4()}", task_queue=queue,
        )
        await approved.signal(
            IncidentWorkflow.plan_decided, args=["original-approval", True],
        )
        await approved.signal(
            IncidentWorkflow.plan_decided, args=["stale-rejection", False],
        )
        rejected = await env.client.start_workflow(
            IncidentWorkflow.run, args=["tenant", str(uuid4())],
            id=f"rejected-{uuid4()}", task_queue=queue,
        )
        await rejected.signal(
            IncidentWorkflow.plan_decided, args=["first-rejection", False],
        )
        await rejected.signal(
            IncidentWorkflow.plan_decided, args=["later-approval", True],
        )
        async with Worker(
            env.client, task_queue=queue, workflows=[IncidentWorkflow],
            activities=[project, route, inspect, apply],
        ):
            assert await approved.result() == "VERIFIED"
            assert await rejected.result() == "NEEDS_REVIEW"

    assert applied == ["original-approval"]
