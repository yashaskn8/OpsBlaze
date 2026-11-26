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

from backend.adapters.simulator import SimulatorAdapter
from backend.database import get_session_maker
from backend.effects.gateway import reconcile_effect
from backend.lifecycle import UNRESOLVED_EFFECT_STATES
from backend.models import EffectReceipt, Incident, OutboxEntry
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


async def reconcile_unresolved_effects(
    sessions: async_sessionmaker[AsyncSession],
    adapter: SimulatorAdapter,
    *,
    limit: int = 20,
) -> dict:
    """B3-06: bounded read-only reconciliation poller for unresolved receipts."""
    if not 1 <= limit <= 100:
        raise ValueError("scan bounds must be positive and limited")
    async with sessions() as session:
        effect_ids = list((await session.scalars(
            select(EffectReceipt.id)
            .where(EffectReceipt.state.in_(UNRESOLVED_EFFECT_STATES))
            .order_by(EffectReceipt.updated_at, EffectReceipt.id)
            .limit(limit)
        )).all())

    reconciled_verified = 0
    still_unresolved = 0
    needs_review = 0
    errors = 0
    for effect_id in effect_ids:
        try:
            outcome = await reconcile_effect(sessions, adapter, effect_id)
        except Exception as exc:
            errors += 1
            log.warning(
                "Effect reconciliation deferred: effect_id=%s class=%s",
                effect_id, type(exc).__name__,
            )
            continue
        if outcome == "VERIFIED":
            reconciled_verified += 1
        elif outcome == "NEEDS_REVIEW":
            needs_review += 1
        else:
            still_unresolved += 1

    return {
        "scanned": len(effect_ids),
        "reconciled_verified": reconciled_verified,
        "still_unresolved": still_unresolved,
        "needs_review": needs_review,
        "errors": errors,
        "provider_success_claimed": False,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Temporal projection audit and bounded effect reconciliation"
    )
    parser.add_argument("--mode", choices=("temporal", "effects"), default="temporal")
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = get_settings()
    sessions = get_session_maker()
    if args.mode == "effects":
        summary = await reconcile_unresolved_effects(
            sessions, SimulatorAdapter(sessions), limit=args.limit,
        )
        print(json.dumps(summary, indent=2))
        return
    client = await connect_temporal(settings)
    issues = await compare_temporal_projection(
        sessions, client,
        stale_seconds=args.stale_seconds, limit=args.limit,
    )
    print(json.dumps({"issues": issues, "count": len(issues)}, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
