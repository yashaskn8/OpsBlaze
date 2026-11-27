"""Claim pending intents in short PostgreSQL transactions, then dispatch outside DB."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.database import get_session_maker
from backend.settings import get_settings
from backend.telemetry import initialize_tracing, trace_operation
from backend.trace_refs import admission_parent
from backend.temporal_connection import connect_temporal
from backend.workflows.incident import IncidentWorkflow
from backend.workers.heartbeat import heartbeat_loop
from backend.workers.lineage_recovery import lineage_recovery_loop
from backend.workers.outbox import (
    Claim, acknowledge_claim, claim_intent, claim_intent_batch,
)

log = logging.getLogger(__name__)
DELIVERY_TIMEOUT_SECONDS = 5


def workflow_id(tenant_id: str, incident_id: UUID) -> str:
    return f"opsblaze/{tenant_id}/{incident_id}"


async def deliver_intent(client: Client, intent: tuple[int, str, str, str, dict | None]) -> None:
    _, tenant_id, incident_id, kind, payload = intent[:5]
    wid = workflow_id(tenant_id, UUID(incident_id))
    if kind == "START_WORKFLOW":
        try:
            await client.start_workflow(
                IncidentWorkflow.run,
                args=[tenant_id, incident_id],
                id=wid,
                task_queue=get_settings().temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            # Covers an acknowledgment lost after a successful Temporal start.
            pass
    elif kind == "CANCEL_WORKFLOW":
        await client.get_workflow_handle(wid).signal(
            IncidentWorkflow.request_cancel
        )
    elif kind == "ACKNOWLEDGE_REVIEW":
        await client.get_workflow_handle(wid).signal(
            IncidentWorkflow.acknowledge_review
        )
    elif kind == "PLAN_DECIDED":
        if (
            not isinstance(payload, dict)
            or set(payload) != {"plan_id", "approved"}
            or not isinstance(payload.get("plan_id"), str)
            or type(payload.get("approved")) is not bool
        ):
            raise ValueError("plan signal must have a canonical typed payload")
        try:
            plan_id = UUID(payload["plan_id"])
        except ValueError as exc:
            raise ValueError("plan signal has an invalid plan ID") from exc
        if str(plan_id) != payload["plan_id"]:
            raise ValueError("plan signal ID is not canonical")
        await client.get_workflow_handle(wid).signal(
            IncidentWorkflow.plan_decided,
            args=[str(plan_id), payload["approved"]],
        )
    else:
        raise ValueError("unsupported outbox kind")


async def _deliver_and_ack(
    client: Client, sessions: async_sessionmaker[AsyncSession], intent: Claim,
) -> None:
    entry_id = intent[0]
    try:
        incident_id = UUID(intent[2])
        parent = await admission_parent(
            sessions, intent[1], incident_id,
            enabled=bool(get_settings().otel_traces_endpoint),
        )
        with trace_operation(
            "opsblaze.outbox.deliver", incident_id=incident_id,
            event_id=entry_id, parent=parent,
        ):
            async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                await deliver_intent(client, intent)
    except Exception as exc:
        # Temporal exception text may include sensitive payloads. Persist a
        # safe class label only; never log the exception or traceback.
        log.warning(
            "Temporal outbox delivery deferred: intent_id=%s class=%s",
            entry_id, type(exc).__name__,
        )
        # Preserve the leased intent for durable retry; no false ACK.
        return
    try:
        await acknowledge_claim(sessions, intent)
    except Exception as exc:
        # Temporal may have committed while PostgreSQL was unavailable.
        # Preserve the row for stable-ID retry; do not reissue an effect.
        log.warning(
            "Temporal outbox acknowledgment deferred: intent_id=%s class=%s",
            entry_id, type(exc).__name__,
        )


async def dispatch_once(
    client: Client, sessions: async_sessionmaker[AsyncSession],
    *, tenant_filter: str | None = None,
) -> bool:
    try:
        intent = await claim_intent(sessions, tenant_filter=tenant_filter)
    except Exception as exc:
        # PostgreSQL may be temporarily unreachable. A claim failure must
        # not terminate the dispatcher or invent a successful delivery.
        log.warning(
            "Temporal outbox claim deferred: class=%s",
            type(exc).__name__,
        )
        return False
    if intent is None:
        return False
    await _deliver_and_ack(client, sessions, intent)
    return True


async def dispatch_batch(
    client: Client, sessions: async_sessionmaker[AsyncSession],
    *, tenant_filter: str | None = None, limit: int = 10,
) -> int:
    try:
        intents = await claim_intent_batch(
            sessions, tenant_filter=tenant_filter, limit=limit,
        )
    except Exception as exc:
        log.warning(
            "Temporal outbox batch claim deferred: class=%s",
            type(exc).__name__,
        )
        return 0
    for intent in intents:
        await _deliver_and_ack(client, sessions, intent)
    return len(intents)


async def main() -> None:
    settings = get_settings()
    initialize_tracing(settings)
    client = await connect_temporal(settings)
    sessions = get_session_maker()
    log.info("Outbox dispatcher started")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(heartbeat_loop("dispatcher"))
        tasks.create_task(lineage_recovery_loop(sessions))
        while True:
            claimed = await dispatch_batch(client, sessions, limit=10)
            if claimed == 0:
                await asyncio.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
