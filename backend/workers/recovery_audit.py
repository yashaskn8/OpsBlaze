"""G3 PostgreSQL/Temporal recovery-consistency audit: strictly read-only.

A missing Temporal description may mean an outage, retention or history
loss. A local receipt cannot authorize success without independent witness.
This command never dispatches, ACKs, repairs, or changes any persisted row.
"""

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from backend.database import get_session_maker
from backend.models import EffectReceipt, Incident, OutboxEntry
from backend.settings import get_settings
from backend.temporal_connection import connect_temporal
from backend.workers.dispatcher import workflow_id

TERMINAL_WORKFLOWS = frozenset({
    "COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT",
})
KNOWN_WORKFLOWS = TERMINAL_WORKFLOWS | {"RUNNING"}
ACTIVE_PROJECTIONS = frozenset({
    "DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
    "PLAN_DECIDED", "EXECUTING", "CANCEL_REQUESTED",
    "REVIEW_ACKNOWLEDGED",
})
UNRESOLVED_RECEIPTS = frozenset({"SENT", "UNKNOWN", "CONFIRMED"})
VALID_RECEIPTS = frozenset({
    "PREPARED", "SENT", "UNKNOWN", "CONFIRMED",
    "VERIFIED", "FAILED", "NEEDS_REVIEW",
})


def check_receipt(
    state: str, adapter: str, permit_digest: str | None,
    attempts: int, expected_version: int, observed_version: int | None,
) -> str | None:
    """Flag unsupported effect invariants without querying an adapter."""
    if state not in VALID_RECEIPTS or adapter != "simulator":
        return "RECEIPT_INVARIANT_VIOLATION"
    if (
        not isinstance(permit_digest, str) or len(permit_digest) != 64
        or any(c not in "0123456789abcdef" for c in permit_digest)
        or type(attempts) is not int or attempts < 0
        or type(expected_version) is not int or expected_version < 0
    ):
        return "RECEIPT_INVARIANT_VIOLATION"
    if state == "PREPARED":
        return (
            "RECEIPT_INVARIANT_VIOLATION"
            if attempts != 0 or observed_version is not None else None
        )
    if state in {"SENT", "UNKNOWN", "CONFIRMED", "VERIFIED"}:
        if attempts != 1:
            return "RECEIPT_INVARIANT_VIOLATION"
        if state in {"CONFIRMED", "VERIFIED"} and (
            type(observed_version) is not int
            or observed_version != expected_version + 1
        ):
            return "RECEIPT_INVARIANT_VIOLATION"
        if state in {"SENT", "UNKNOWN"} and observed_version is not None and (
            type(observed_version) is not int
            or observed_version != expected_version + 1
        ):
            return "RECEIPT_INVARIANT_VIOLATION"
        return "UNRESOLVED_EFFECT_REQUIRES_RECONCILIATION" if (
            state in UNRESOLVED_RECEIPTS
        ) else None
    return None


async def _temporal_description(
    client: Client, wid: str,
) -> tuple[str, bool]:
    """Return a bounded, non-authoritative Temporal view and identity check."""
    try:
        desc = await asyncio.wait_for(
            client.get_workflow_handle(wid).describe(), timeout=3,
        )
    except Exception:
        # Never treat a timeout, missing history or transport failure as
        # proof the start was not committed.
        return "UNKNOWN", False

    status = getattr(desc.status, "name", desc.status)
    if not isinstance(status, str) or status not in KNOWN_WORKFLOWS:
        return "UNKNOWN", False

    # The SDK normally includes ID/type; absence is not a positive match.
    observed_id = getattr(desc, "id", None)
    observed_type = getattr(desc, "workflow_type", None)
    if observed_id != wid or observed_type != "IncidentWorkflow":
        return status, False
    return status, True


async def audit_recovery_consistency(
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    *,
    stale_seconds: int = 300,
    limit: int = 20,
    now: datetime | None = None,
) -> dict:
    """Bounded scan with no remote mutations and no database write sessions.

    Only durable starts attempted at least once or already ACKed are
    considered; a fresh never-attempted START is not a Temporal failure.
    """
    if (
        type(stale_seconds) is not int or not 1 <= stale_seconds <= 86400
        or type(limit) is not int or not 1 <= limit <= 50
    ):
        raise ValueError("audit scan bounds out of range")
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("audit observation time must include timezone")
    cutoff = observed.astimezone(timezone.utc) - timedelta(seconds=stale_seconds)

    async with sessions() as session:
        starts = (await session.execute(
            select(
                Incident.tenant_id, Incident.id, Incident.status_projection,
                OutboxEntry.id, OutboxEntry.attempt,
                OutboxEntry.delivered_at, OutboxEntry.dead_lettered_at,
            ).join(OutboxEntry, OutboxEntry.incident_id == Incident.id)
            .where(
                OutboxEntry.kind == "START_WORKFLOW",
                OutboxEntry.created_at <= cutoff,
                or_(
                    OutboxEntry.attempt > 0,
                    OutboxEntry.delivered_at.is_not(None),
                ),
            )
            .order_by(OutboxEntry.id).limit(limit + 1)
        )).all()

        selected = starts[:limit]
        incident_ids = list({row[1] for row in selected})
        receipts = []
        if incident_ids:
            receipts = (await session.execute(
                select(
                    EffectReceipt.incident_id, EffectReceipt.tenant_id,
                    EffectReceipt.state, EffectReceipt.adapter_id,
                    EffectReceipt.permit_digest, EffectReceipt.attempt_count,
                    EffectReceipt.expected_version, EffectReceipt.observed_version,
                ).where(EffectReceipt.incident_id.in_(incident_ids))
                .order_by(EffectReceipt.created_at, EffectReceipt.id)
                .limit(limit * 8 + 1)
            )).all()

    receipt_overflow = len(receipts) > limit * 8
    by_incident: dict[UUID, list] = defaultdict(list)
    for receipt in receipts[:limit * 8]:
        by_incident[receipt[0]].append(receipt)
    start_counts: dict[UUID, int] = defaultdict(int)
    for row in selected:
        start_counts[row[1]] += 1

    results = []
    # Bound concurrent Temporal reads, and never hold a DB transaction
    # across a network RPC.
    semaphore = asyncio.Semaphore(4)

    async def inspect(row) -> dict:
        tenant, incident_id, projection, entry_id, attempts, delivered, dead = row
        wid = workflow_id(tenant, incident_id)
        async with semaphore:
            status, identity_ok = await _temporal_description(client, wid)
        reasons = set()
        if start_counts[incident_id] > 1:
            reasons.add("DUPLICATE_START_INTENT")
        if type(attempts) is not int or attempts < 0:
            reasons.add("INVALID_START_ATTEMPT")
        if delivered is not None and dead is not None:
            reasons.add("CONFLICTING_OUTBOX_TERMINAL_MARKERS")
        if status == "UNKNOWN":
            reasons.add("TEMPORAL_HISTORY_UNAVAILABLE_OR_UNKNOWN")
        elif not identity_ok:
            reasons.add("TEMPORAL_HISTORY_IDENTITY_UNVERIFIED")
        elif delivered is None:
            reasons.add("TEMPORAL_START_UNACKNOWLEDGED_AFTER_REMOTE_COMMIT")
        elif status in TERMINAL_WORKFLOWS and projection in ACTIVE_PROJECTIONS:
            reasons.add("PROJECTION_LAGS_TERMINAL_HISTORY")
        elif status == "RUNNING" and projection not in ACTIVE_PROJECTIONS:
            reasons.add("TERMINAL_PROJECTION_WHILE_TEMPORAL_RUNNING")

        if delivered is None and status == "UNKNOWN":
            reasons.add("UNACKNOWLEDGED_START_CANNOT_BE_SAFELY_REPLAYED_FROM_AUDIT")
        if receipt_overflow:
            reasons.add("RECEIPT_SCAN_TRUNCATED")
        for receipt in by_incident[incident_id]:
            _, receipt_tenant, state, adapter, digest, count, version, observed_v = receipt
            if receipt_tenant != tenant:
                reasons.add("CROSS_TENANT_RECEIPT")
                continue
            finding = check_receipt(
                state, adapter, digest, count, version, observed_v,
            )
            if finding is not None:
                reasons.add(finding)
        return {
            "incident_id": str(incident_id),
            "outbox_id": entry_id,
            "projection": projection,
            "temporal_status": status,
            "temporal_identity_verified": identity_ok,
            "start_acknowledged": delivered is not None,
            "reasons": sorted(reasons),
            "safe_action": "INSPECT_ONLY",
        }

    if selected:
        results = list(await asyncio.gather(*(inspect(row) for row in selected)))
    return {
        "status": "AUDIT_ONLY",
        "scanned": len(selected),
        "scan_truncated": len(starts) > limit,
        "receipt_scan_truncated": receipt_overflow,
        "findings": results,
        "external_effects_enabled": False,
        "recovery_certified": False,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only PostgreSQL/Temporal recovery consistency audit"
    )
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    settings = get_settings()
    client = await connect_temporal(settings)
    report = await audit_recovery_consistency(
        get_session_maker(), client,
        stale_seconds=args.stale_seconds, limit=args.limit,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["scan_truncated"] or report["receipt_scan_truncated"]:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
