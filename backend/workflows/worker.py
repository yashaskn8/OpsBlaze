"""Temporal worker factory for OpsBlaze Feature B1 and Stage 3."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from backend.adapters.protocol import TargetAdapterProtocol
from backend.investigator.fake import FakeInvestigator
from backend.investigator.planner import AdaptiveProbePlanner
from backend.workflows.activities import B1_TASK_QUEUE, B1Activities
from backend.workflows.incident_workflow import IncidentWorkflow


def create_b1_worker(
    client: Client,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    task_queue: str = B1_TASK_QUEUE,
    investigator: FakeInvestigator | None = None,
    adapters: dict[str, TargetAdapterProtocol] | None = None,
    planner: AdaptiveProbePlanner | None = None,
) -> Worker:
    """Create a configured Temporal Worker for B1 + Stage 3 + Stage 5 IncidentWorkflow and activities."""
    activities = B1Activities(
        session_factory=session_factory,
        investigator=investigator,
        adapters=adapters,
        planner=planner,
    )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[IncidentWorkflow],
        activities=[
            activities.project_incident_status,
            activities.run_fake_investigation,
            activities.run_adaptive_investigation,
            activities.execute_approved_action,
            activities.verify_and_finalize_effect,
        ],
    )
