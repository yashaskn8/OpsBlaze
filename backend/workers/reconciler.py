"""B1 conservative anti-entropy report for PostgreSQL versus Temporal.

Read-only: an aged projection or unavailable Temporal history is evidence
for investigation, not authority to start a second workflow or invent success.
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from backend.database import get_session_maker
from backend.models import Incident, OutboxEntry
from backend.settings import get_settings
from backend.temporal_connection import connect_temporal
from backend.workers.dispatcher import workflow_id

log = logging.getLogger(__name__)

STUCK_STATES = frozenset({
    "DISPATCH_PENDING", "INVESTIGATING", "PLAN_DECIDED",
    "EXECUTING", "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED",
})
FINAL_TEMPORAL = frozenset({
    "COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT",
})


async def compare_temporal_projection(
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    stale_seconds: int = 300,
    limit: int = 20,
) -> list[dict]:
    if stale_seconds < 1 or not 1 <= limit <= 100:
        raise ValueError("scan bounds must be positive and limited")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    async with sessions() as session:
        rows = (await session.execute(
            select(Incident.tenant_id, Incident.id, Incident.status_projection)
            .join(OutboxEntry, OutboxEntry.incident_id == Incident.id)
            .where(
                OutboxEntry.kind == "START_WORKFLOW",
                OutboxEntry.delivered_at.is_not(None),
                Incident.phase_entered_at < cutoff,
                Incident.status_projection.in_(STUCK_STATES),
            )
            .order_by(Incident.phase_entered_at, Incident.id)
            .limit(limit)
        )).all()

    issues: list[dict] = []
    for tenant_id, incident_id, projected in rows:
        wid = workflow_id(tenant_id, incident_id)
        try:
            description = await asyncio.wait_for(
                client.get_workflow_handle(wid).describe(), timeout=3,
            )
            state = getattr(description.status, "name", str(description.status))
        except Exception:
            # Network errors and missing history are not conflated with
            # a confirmed terminal state; no recovery action is triggered.
            state = "UNKNOWN"

        if state in FINAL_TEMPORAL:
            issue = "PROJECTION_LAGS_TERMINAL_HISTORY"
        elif state == "RUNNING":
            issue = "AGED_RUNNING_WORKFLOW"
        else:
            issue = "TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN"
        issues.append({
            "incident_id": str(incident_id),
            "tenant_id": tenant_id,
            "projection": projected,
            "temporal_status": state,
            "reason": issue,
            "safe_action": "INSPECT_ONLY",
        })
    return issues


async def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Temporal projection audit")
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = get_settings()
    client = await connect_temporal(settings)
    issues = await compare_temporal_projection(
        get_session_maker(), client,
        stale_seconds=args.stale_seconds, limit=args.limit,
    )
    print(json.dumps({"issues": issues, "count": len(issues)}, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
