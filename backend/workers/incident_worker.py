"""Run in a separate process from API and outbox dispatcher."""

import asyncio
import logging
from contextlib import suppress

from temporalio.client import Client
from temporalio.worker import Worker

from backend.settings import get_settings
from backend.workflows.activities import (
    inspect_incident_read_only,
    apply_approved_simulator_plan,
    select_incident_route,
    project_incident_status,
)
from backend.workflows.incident import IncidentWorkflow
from backend.workers.heartbeat import heartbeat_loop


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[IncidentWorkflow],
        activities=[inspect_incident_read_only, project_incident_status, apply_approved_simulator_plan, select_incident_route],
    )
    heartbeat = asyncio.create_task(heartbeat_loop("incident_worker"))
    try:
        await worker.run()
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
