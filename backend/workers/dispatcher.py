"""Claim pending intents in short PostgreSQL transactions, then dispatch outside DB."""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.database import get_session_maker
from backend.models import Incident, OutboxEntry
from backend.settings import get_settings
from backend.temporal_connection import connect_temporal
from backend.workflows.incident import IncidentWorkflow
from backend.workers.heartbeat import heartbeat_loop

log = logging.getLogger(__name__)


def workflow_id(tenant_id: str, incident_id: UUID) -> str:
    return f"opsblaze/{tenant_id}/{incident_id}"


async def claim_intent(
    sessions: async_sessionmaker[AsyncSession],
    tenant_filter: str | None = None,
) -> tuple[int, str, str, str, dict | None] | None:
    now = datetime.now(timezone.utc)
    start = aliased(OutboxEntry)
    older = aliased(OutboxEntry)
    earlier_pending = (
        select(older.id)
        .where(
            older.incident_id == OutboxEntry.incident_id,
            older.id < OutboxEntry.id,
            older.delivered_at.is_(None),
        )
        .correlate(OutboxEntry)
        .exists()
    )
    start_delivered = (
        select(start.id)
        .where(
            start.incident_id == OutboxEntry.incident_id,
            start.kind == "START_WORKFLOW",
            start.delivered_at.is_not(None),
        )
        .correlate(OutboxEntry)
        .exists()
    )
    async with sessions() as session:
        async with session.begin():
            stmt = (
                select(OutboxEntry, Incident.tenant_id)
                .join(Incident, Incident.id == OutboxEntry.incident_id)
                .where(
                    OutboxEntry.delivered_at.is_(None),
                    OutboxEntry.available_at <= now,
                    # Preserve per-incident signal ordering across parallel
                    # dispatchers, including lost-ack leases and retries.
                    ~earlier_pending,
                    # A signal may not overtake the stable workflow start,
                    # even when another dispatcher leases the older row.
                    or_(OutboxEntry.kind == "START_WORKFLOW", start_delivered),
                )
                .order_by(OutboxEntry.id)
                .limit(1)
                .with_for_update(of=OutboxEntry, skip_locked=True)
            )
            if tenant_filter is not None:
                stmt = stmt.where(Incident.tenant_id == tenant_filter)
            result = (await session.execute(stmt)).first()
            if result is None:
                return None
            entry, tenant_id = result
            entry.attempt += 1
            # Lease the claim before making a network call, avoiding a long
            # transaction. After a crash the intent becomes eligible again.
            retry_seconds = min(300, 2 ** min(entry.attempt, 8))
            entry.available_at = now + timedelta(seconds=retry_seconds)
            return (entry.id, tenant_id, str(entry.incident_id), entry.kind, entry.payload)


async def deliver_intent(client: Client, intent: tuple[int, str, str, str, dict | None]) -> None:
    _, tenant_id, incident_id, kind, payload = intent
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
        if intent[3] == "PROJECT_EXPERIENCE":
            from backend.workers.experience_projector import project_experience_outbox

            result = await project_experience_outbox(
                sessions, intent[1], intent[2], intent[4],
            )
            if result != "CAPTURED":
                log.info(
                    "E1 projection intentionally skipped: intent_id=%s state=%s",
                    entry_id, result,
                )
        else:
            await deliver_intent(client, intent)
    except Exception:
        log.exception("Outbox dispatch failed: intent_id=%s", entry_id)
        # The lease ensures bounded retry; never drop the intent.
        return True
    async with sessions() as session:
        async with session.begin():
            entry = await session.get(OutboxEntry, entry_id)
            if entry is not None and entry.delivered_at is None:
                entry.delivered_at = datetime.now(timezone.utc)
    return True


async def main() -> None:
    settings = get_settings()
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
