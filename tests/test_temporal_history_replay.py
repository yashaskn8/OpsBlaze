"""B1 replay a serialized native Temporal history with the current workflow.

This catches non-deterministic code changes against representative fresh
histories. CI can retain version-bound synthetic fixtures for later replay.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.workflows.incident import IncidentWorkflow
from backend.workflows.replay_archive import replay_archive, write_archive


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_temporal_histories_survive_json_roundtrip_and_replay(tmp_path):
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
        return "VERIFIED"

    histories = []
    async with await WorkflowEnvironment.start_local() as env:
        queue = f"opsblaze-replay-{uuid4()}"
        async with Worker(
            env.client, task_queue=queue, workflows=[IncidentWorkflow],
            activities=[project, route, inspect, apply],
        ):
            for branch, expected in (
                ("approve", "VERIFIED"),
                ("reject", "NEEDS_REVIEW"),
                ("cancel", "CANCELLED"),
            ):
                wid = f"replay-{branch}-{uuid4()}"
                handle = await env.client.start_workflow(
                    IncidentWorkflow.run,
                    args=["tenant", branch], id=wid, task_queue=queue,
                )
                if branch == "cancel":
                    await handle.signal(IncidentWorkflow.request_cancel)
                else:
                    await handle.signal(
                        IncidentWorkflow.plan_decided,
                        args=["fixture-plan", branch == "approve"],
                    )
                assert await handle.result() == expected
                history = await handle.fetch_history()
                # Test the same JSON export/import path used for archived
                # histories; the file is temporary and contains no secrets.
                path = tmp_path / f"{branch}.json"
                path.write_text(history.to_json(), encoding="utf-8")
                histories.append((wid, path))

    archive_path = os.environ.get("OPSBLAZE_SYNTHETIC_REPLAY_ARCHIVE")
    if archive_path:
        directory = Path(archive_path)
        source_commit = os.environ["GITHUB_SHA"]
        write_archive(directory, source_commit, {
            path.stem: (workflow_id, path.read_text(encoding="utf-8"))
            for workflow_id, path in histories
        })
        await replay_archive(directory, source_commit)
        return

    replayer = Replayer(workflows=[IncidentWorkflow])
    for workflow_id, path in histories:
        exported = path.read_text(encoding="utf-8")
        roundtripped = WorkflowHistory.from_json(workflow_id, exported)
        assert roundtripped.events
        await replayer.replay_workflow(roundtripped)
