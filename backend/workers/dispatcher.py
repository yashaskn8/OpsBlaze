"""Claim pending intents in short PostgreSQL transactions, then dispatch outside DB."""

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

  from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.database import get_session_maker
 from backend.settings import get_settings
from backend.telemetry import initialize_tracing, trace_operation
from backend.temporal_connection import connect_temporal
from backend.workflows.incident import IncidentWorkflow
from backend.workers.heartbeat import heartbeat_loop
from backend.workers.outbox import acknowledge_claim, claim_intent

log = logging.getLogger(__name__)


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


async def dispatch_once(
    client: Client, sessions: async_sessionmaker[AsyncSession],
    *, tenant_filter: str | None = None,
) -> bool:
    intent = await claim_intent(sessions, tenant_filter=tenant_filter)
    if intent is None:
        return False
    entry_id = intent[0]
    try:
        with trace_operation(
            "opsblaze.outbox.deliver", incident_id=UUID(intent[2]),
            event_id=entry_id,
        ):
            await deliver_intent(client, intent)
    except Exception:
        log.exception("Temporal outbox dispatch failed: intent_id=%s", entry_id)
        # Preserve the leased intent for durable retry; no false ACK.
        return True
    await acknowledge_claim(sessions, intent)

    async with sessions() as session:
        async with session.begin():
            entry = await session.get(OutboxEntry, entry_id)
            if (
                entry is not None
                and entry.delivered_at is None
                and entry.dead_lettered_at is None
                and entry.attempt == intent[5]
            ):
                # Older workers must not acknowledge a newer lease's attempt.
                entry.delivered_at = datetime.now(timezone.utc)
    return True


async def main() -> None:
    settings = get_settings()
    initialize_tracing(settings)
    client = await connect_temporal(settings)
    sessions = get_session_maker()
    heartbeat = asyncio.create_task(heartbeat_loop("dispatcher"))
    log.info("Outbox dispatcher started")
    try:
        while True:
            found = await dispatch_once(client, sessions)
            if not found:
                await asyncio.sleep(1)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
