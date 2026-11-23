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

from sqlalchemy import func, or_, select
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
    if state == "NEEDS_REVIEW":
        return "EFFECT_REQUIRES_HUMAN_REVIEW"
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

    # A transport may return an incomplete or malformed description. Never
    # let an unexpected response turn a bounded audit into an uncaught error.
    try:
        raw_status = getattr(desc, "status", None)
        status = getattr(raw_status, "name", raw_status)
        if not isinstance(status, str) or status not in KNOWN_WORKFLOWS:
            return "UNKNOWN", False

        # The SDK normally includes ID/type; absence is not a positive match.
        observed_id = getattr(desc, "id", None)
        observed_type = getattr(desc, "workflow_type", None)
        if observed_id != wid or observed_type != "IncidentWorkflow":
            return status, False
        return status, True
    except Exception:
        return "UNKNOWN", False


async def audit_recovery_consistency(
    sessions: async_sessionmaker[AsyncSession],
    client: Client,
    *,
    stale_seconds: int = 300,
    limit: int = 20,
    after_id: int = 0,
    through_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Bounded scan with no remote mutations and no database write sessions.

    Only durable starts attempted, ACKed or dead-lettered are considered;
    a fresh never-attempted START is not a Temporal failure.
    """
    if (
        type(stale_seconds) is not int or not 1 <= stale_seconds <= 86400
        or type(limit) is not int or not 1 <= limit <= 50
        or type(after_id) is not int or after_id < 0
        or (through_id is not None and (
            type(through_id) is not int or through_id < 0
            or after_id > through_id
        ))
    ):
        raise ValueError("audit scan bounds out of range")
    observed = datetime.now(timezone.utc) if now is None else now
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("audit observation time must include timezone")
    cutoff = observed.astimezone(timezone.utc) - timedelta(seconds=stale_seconds)

    async with sessions() as session:
        # Pin a high-water mark on the first page. Subsequent pages must
        # supply this returned value to avoid chasing newly inserted starts.
        if through_id is None:
            through_id = await session.scalar(select(func.max(OutboxEntry.id))) or 0
        starts = (await session.execute(
            select(
                Incident.tenant_id, Incident.id, Incident.status_projection,
                OutboxEntry.id, OutboxEntry.attempt,
                OutboxEntry.delivered_at, OutboxEntry.dead_lettered_at,
            ).join(OutboxEntry, OutboxEntry.incident_id == Incident.id)
            .where(
                OutboxEntry.kind == "START_WORKFLOW",
                OutboxEntry.id > after_id,
                OutboxEntry.id <= through_id,
                OutboxEntry.created_at <= cutoff,
                or_(
                    OutboxEntry.attempt > 0,
                    OutboxEntry.delivered_at.is_not(None),
                    OutboxEntry.dead_lettered_at.is_not(None),
                ),
            )
            .order_by(OutboxEntry.id).limit(limit + 1)
        )).all()

        selected = starts[:limit]
        incident_ids = list({row[1] for row in selected})
        start_counts = {}
        if incident_ids:
            counts = (await session.execute(
                select(OutboxEntry.incident_id, func.count(OutboxEntry.id))
                .where(
                    OutboxEntry.kind == "START_WORKFLOW",
                    OutboxEntry.incident_id.in_(incident_ids),
                )
                .group_by(OutboxEntry.incident_id)
            )).all()
            start_counts = dict(counts)
        receipt_counts = {}
        receipts = []
        if incident_ids:
            receipt_counts = dict((await session.execute(
                select(EffectReceipt.incident_id, func.count(EffectReceipt.id))
                .where(EffectReceipt.incident_id.in_(incident_ids))
                .group_by(EffectReceipt.incident_id)
            )).all())
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
        if start_counts.get(incident_id, 0) > 1:
            reasons.add("DUPLICATE_START_INTENT")
        if type(attempts) is not int or attempts < 0:
            reasons.add("INVALID_START_ATTEMPT")
        elif delivered is not None and attempts == 0:
            reasons.add("ACKNOWLEDGED_START_WITHOUT_ATTEMPT")
        if delivered is not None and dead is not None:
            reasons.add("CONFLICTING_OUTBOX_TERMINAL_MARKERS")
        if dead is not None and delivered is None:
            reasons.add("DEAD_LETTERED_START_REQUIRES_OPERATOR_REVIEW")
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
        if receipt_counts.get(incident_id, 0) > len(by_incident[incident_id]):
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
        "observed_at": observed.astimezone(timezone.utc).isoformat(),
        "scanned": len(selected),
        "scan_truncated": len(starts) > limit,
        "after_id": after_id,
        "through_id": through_id,
        "next_after_id": selected[-1][3] if len(starts) > limit else None,
        "receipt_scan_truncated": receipt_overflow,
        "findings": results,
        "external_effects_enabled": False,
        "recovery_certified": False,
    }


def audit_exit_code(report: dict) -> int:
    """Signal incomplete scans and findings to unattended audit callers."""
    if report["scan_truncated"] or report["receipt_scan_truncated"]:
        return 2
    if any(finding["reasons"] for finding in report["findings"]):
        return 1
    return 0


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only PostgreSQL/Temporal recovery consistency audit"
    )
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--through-id", type=int, default=None)
    parser.add_argument(
        "--observed-at", type=str,
        help="Fixed timezone-aware ISO-8601 audit observation time; reuse across pages",
    )
    args = parser.parse_args()
    observed_at = None
    if args.observed_at is not None:
        try:
            observed_at = datetime.fromisoformat(args.observed_at)
        except ValueError as exc:
            parser.error(f"invalid --observed-at ISO-8601 timestamp: {exc}")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            parser.error("--observed-at requires a timezone offset")
    settings = get_settings()
    client = await connect_temporal(settings)
    report = await audit_recovery_consistency(
        get_session_maker(), client,
        stale_seconds=args.stale_seconds, limit=args.limit,
        after_id=args.after_id, through_id=args.through_id,
        now=observed_at,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(audit_exit_code(report))


if __name__ == "__main__":
    asyncio.run(main())
