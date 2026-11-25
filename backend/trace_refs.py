"""Tenant-scoped, bounded access to local recorded-span references."""

import logging
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Incident, OutcomeRevision, TraceRef
from backend.trace_identity import valid_hex_id

log = logging.getLogger(__name__)


async def store_trace_ref(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    record_type: str, record_id: UUID, ids: tuple[str, str] | None,
) -> None:
    """After the authoritative record is flushed, isolate optional writes in a savepoint."""
    if ids is None:
        return
    trace_id, span_id = ids
    if (
        record_type not in {"incident", "outcome"}
        or (record_type == "incident" and record_id != incident_id)
        or not valid_hex_id(trace_id, 32)
        or not valid_hex_id(span_id, 16)
    ):
        return
    try:
        async with session.begin_nested():
            session.add(TraceRef(
                tenant_id=tenant_id, incident_id=incident_id,
                record_type=record_type, record_id=record_id,
                trace_id=trace_id, span_id=span_id,
            ))
            await session.flush()
    except SQLAlchemyError as exc:
        # The diagnostic write cannot turn a healthy primary transaction into
        # a failed admission or witness. A lost DB connection still fails the
        # primary transaction normally.
        log.warning("Optional trace reference unavailable: class=%s", type(exc).__name__)


def scoped_trace_refs(tenant_id: str, incident_id: UUID):
    """Verify the underlying source still exists in exactly the same scope."""
    outcome_exists = select(OutcomeRevision.id).where(
        OutcomeRevision.id == TraceRef.record_id,
        OutcomeRevision.tenant_id == tenant_id,
        OutcomeRevision.incident_id == incident_id,
    ).correlate(TraceRef).exists()
    return select(TraceRef).join(Incident, Incident.id == TraceRef.incident_id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
        TraceRef.tenant_id == tenant_id, TraceRef.incident_id == incident_id,
        or_(
            and_(TraceRef.record_type == "incident", TraceRef.record_id == incident_id),
            and_(TraceRef.record_type == "outcome", outcome_exists),
        ),
    )


def redacted_ref(row: TraceRef) -> dict | None:
    if not valid_hex_id(row.trace_id, 32) or not valid_hex_id(row.span_id, 16):
        return None
    return {
        "node_id": f"{row.record_type}:{row.record_id}",
        "trace_id": row.trace_id, "span_id": row.span_id,
        "collector_verified": False,
    }


async def graph_trace_refs(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    nodes: list[dict],
) -> list[dict]:
    # Only attach links to the bounded, already tenant-checked graph nodes.
    eligible = {node["id"] for node in nodes if node["kind"] in {"incident", "outcome"}}
    if not eligible:
        return []
    record_ids = [UUID(node_id.split(":", 1)[1]) for node_id in eligible]
    rows = (await session.scalars(scoped_trace_refs(tenant_id, incident_id).where(
        TraceRef.record_id.in_(record_ids),
    ).order_by(TraceRef.id))).all()
    return [
        ref for row in rows
        if (ref := redacted_ref(row)) is not None and ref["node_id"] in eligible
    ]


async def read_trace(
    session: AsyncSession, tenant_id: str, incident_id: UUID, trace_id: str,
    cursor: UUID | None = None, limit: int = 50,
) -> dict | None:
    if not valid_hex_id(trace_id, 32) or not 1 <= limit <= 50:
        raise ValueError("invalid trace lookup")
    stmt = scoped_trace_refs(tenant_id, incident_id).where(TraceRef.trace_id == trace_id)
    if cursor is not None:
        stmt = stmt.where(TraceRef.id > cursor)
    rows = (await session.scalars(stmt.order_by(TraceRef.id).limit(limit + 1))).all()
    if not rows:
        return None
    visible = rows[:limit]
    refs = [ref for row in visible if (ref := redacted_ref(row)) is not None]
    return {
        "trace_id": trace_id, "references": refs,
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
        "collector_verified": False,
    }
