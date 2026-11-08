"""Transactional Outbox Dispatcher for OpsBlaze Feature B1."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError

from backend.domain.incident import (
    DegradedRecoveryGate,
    IncidentStatus,
    WorkflowIncidentInput,
    make_incident_workflow_id,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.repositories import ClaimedOutboxItem, OutboxRepository
from backend.workflows.activities import B1_TASK_QUEUE
from backend.workflows.incident_workflow import IncidentWorkflow


class WorkflowStarterProtocol(Protocol):
    async def start_incident_workflow(self, wf_id: str, wf_input: WorkflowIncidentInput) -> str: ...


class TemporalWorkflowStarter:
    """Adapter around temporalio.client.Client for starting IncidentWorkflow."""

    def __init__(self, client: Client, task_queue: str = B1_TASK_QUEUE) -> None:
        self._client = client
        self._task_queue = task_queue

    async def start_incident_workflow(self, wf_id: str, wf_input: WorkflowIncidentInput) -> str:
        try:
            handle = await self._client.start_workflow(
                IncidentWorkflow.run,
                wf_input,
                id=wf_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            return handle.id
        except WorkflowAlreadyStartedError:
            # Stable workflow identity guarantees idempotency when an acknowledgment was lost
            return wf_id


class OutboxDispatcher:
    """Reliable, bounded-concurrency transactional outbox dispatcher for B1."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_starter: WorkflowStarterProtocol,
        *,
        batch_size: int = 10,
        max_concurrency: int = 5,
        lease_seconds: float = 15.0,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        stuck_attempt_threshold: int = 5,
        stuck_age_seconds: float = 60.0,
        adaptive_investigation: bool = False,
    ) -> None:
        self._outbox_repo = OutboxRepository(session_factory)
        self._starter = workflow_starter
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._lease_seconds = lease_seconds
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._stuck_attempt_threshold = stuck_attempt_threshold
        self._stuck_age_seconds = stuck_age_seconds
        self._adaptive_investigation = adaptive_investigation

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        """Claim a batch of eligible outbox items and dispatch them with bounded concurrency.

        Returns the number of items successfully marked delivered in this pass.
        """
        if DegradedRecoveryGate.is_degraded():
            return 0
        claimed = await self._outbox_repo.claim_batch(
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            stuck_attempt_threshold=self._stuck_attempt_threshold,
            stuck_age_seconds=self._stuck_age_seconds,
            now=now,
        )
        if not claimed:
            return 0

        results = await asyncio.gather(
            *(self._process_item_bounded(item, now=now) for item in claimed),
            return_exceptions=False,
        )
        return sum(1 for ok in results if ok)

    async def _process_item_bounded(
        self, item: ClaimedOutboxItem, *, now: datetime | None = None
    ) -> bool:
        async with self._semaphore:
            return await self._process_item(item, now=now)

    async def _process_item(self, item: ClaimedOutboxItem, *, now: datetime | None = None) -> bool:
        wf_id = make_incident_workflow_id(item.tenant_id, item.incident_id)

        # If the incident was already cancelled before dispatch, mark outbox delivered without starting
        if item.incident_status == IncidentStatus.CANCELLED:
            await self._outbox_repo.mark_delivered(
                outbox_id=item.outbox_id,
                incident_id=item.incident_id,
                tenant_id=item.tenant_id,
                now=now,
            )
            return True

        wf_input = WorkflowIncidentInput(
            tenant_id=str(item.tenant_id),
            incident_id=str(item.incident_id),
            target_id=item.target_id,
            request_sha256=item.request_sha256,
            correlation_id=f"outbox-{item.outbox_id}-att-{item.attempt}",
            adaptive_investigation=self._adaptive_investigation,
        )

        try:
            await self._starter.start_incident_workflow(wf_id, wf_input)
            metrics.inc("workflow_started_total")
            emit_event(
                "workflow.started",
                incident_id=item.incident_id,
                workflow_id=wf_id,
                outbox_id=item.outbox_id,
                attempt=item.attempt,
            )
        except WorkflowAlreadyStartedError:
            # Treated as idempotent success: the workflow was already started on a prior attempt
            metrics.inc("workflow_started_total")
            emit_event(
                "workflow.started",
                incident_id=item.incident_id,
                workflow_id=wf_id,
                outbox_id=item.outbox_id,
                attempt=item.attempt,
                idempotent_reattach=True,
            )
        except (TemporalError, RuntimeError, OSError, ValueError) as exc:
            backoff = min(
                self._max_backoff,
                self._base_backoff * (2 ** max(0, item.attempt - 1)),
            )
            await self._outbox_repo.schedule_retry(
                outbox_id=item.outbox_id,
                incident_id=item.incident_id,
                tenant_id=item.tenant_id,
                backoff_seconds=backoff,
                error_reason=f"{type(exc).__name__}: {exc}",
                now=now,
            )
            return False

        await self._outbox_repo.mark_delivered(
            outbox_id=item.outbox_id,
            incident_id=item.incident_id,
            tenant_id=item.tenant_id,
            now=now,
        )
        return True

    async def run_forever(
        self, stop_event: asyncio.Event, poll_interval_seconds: float = 0.25
    ) -> None:
        while not stop_event.is_set():
            try:
                delivered = await self.dispatch_once()
                if delivered > 0:
                    continue
            except (SQLAlchemyError, TemporalError, RuntimeError, OSError) as exc:
                metrics.inc("dispatcher_failures_total")
                emit_event(
                    "outbox.retry",
                    error=f"Dispatcher loop error: {type(exc).__name__}: {exc}",
                    level="ERROR",
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass
