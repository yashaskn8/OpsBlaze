
"""Full Temporal local-server smoke; no Docker or real target connector."""

from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from backend.workflows.incident import IncidentWorkflow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporal_lifecycle_signals_and_replay_safe_activities() -> None:
    states: dict[str, list[str]] = {}
    applied: list[str] = []

    @activity.defn(name="project_incident_status")
    async def project(tenant_id: str, incident_id: str, state: str) -> None:
        states.setdefault(incident_id, []).append(state)

    @activity.defn(name="select_incident_route")
    async def route(tenant_id: str, incident_id: str) -> str:
        return "RUNBOOK"

    @activity.defn(name="inspect_incident_read_only")
    async def inspect(tenant_id: str, incident_id: str) -> str:
        return "ABSTAINED" if incident_id == "abstain" else "SIMULATOR_REVIEW_REQUIRED"

    @activity.defn(name="apply_approved_simulator_plan")
    async def apply(tenant_id: str, incident_id: str, plan_id: str) -> str:
        applied.append(incident_id)
        return "VERIFIED"

    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-test-{uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[IncidentWorkflow],
            activities=[project, route, inspect, apply],
        ):
            approved = await env.client.start_workflow(
                IncidentWorkflow.run, args=["tenant", "approved"],
                id=f"approved-{uuid4()}", task_queue=queue,
            )
            await approved.signal(IncidentWorkflow.plan_decided, args=["plan", True])
            assert await approved.result() == "VERIFIED"
            assert states["approved"][-1] == "VERIFIED"
            assert applied == ["approved"]

            rejected = await env.client.start_workflow(
                IncidentWorkflow.run, args=["tenant", "rejected"],
                id=f"rejected-{uuid4()}", task_queue=queue,
            )
            await rejected.signal(IncidentWorkflow.plan_decided, args=["plan", False])
            assert await rejected.result() == "NEEDS_REVIEW"
            assert "rejected" not in applied

            cancelled = await env.client.start_workflow(
                IncidentWorkflow.run, args=["tenant", "cancelled"],
                id=f"cancelled-{uuid4()}", task_queue=queue,
            )
            await cancelled.signal(IncidentWorkflow.request_cancel)
            assert await cancelled.result() == "CANCELLED"
            assert "cancelled" not in applied

            abstained = await env.client.execute_workflow(
                IncidentWorkflow.run, args=["tenant", "abstain"],
                id=f"abstain-{uuid4()}", task_queue=queue,
            )
            assert abstained == "NEEDS_REVIEW"
            assert "abstain" not in applied
