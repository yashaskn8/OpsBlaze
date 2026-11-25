"""Transactional evidence-edge projection and bounded source-driven repair."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.lineage_projection_refs import RELATIONS, evidence_candidates, projection_status
from backend.lineage_refs import MAX_EVIDENCE_REFS
from backend.models import Evidence, HypothesisStep, Incident, IncidentPlan, LineageEdge


async def replace_evidence_edges(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    kind: str, source: HypothesisStep | IncidentPlan,
) -> None:
    """Caller owns the transaction; source must be new or locked for repair."""
    if kind not in RELATIONS or source.incident_id != incident_id:
        raise ValueError("invalid evidence projection source")
    if kind == "plan" and source.tenant_id != tenant_id:
        raise ValueError("invalid evidence projection scope")
    if await session.scalar(select(Incident.id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
    )) is None:
        raise ValueError("invalid evidence projection scope")
    ids = evidence_candidates(source.evidence_ids)
    known = (await session.scalars(select(Evidence.id).where(
        Evidence.id.in_(ids), Evidence.tenant_id == tenant_id,
        Evidence.incident_id == incident_id,
    ))).all() if ids else []
    await session.execute(delete(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id, LineageEdge.incident_id == incident_id,
        LineageEdge.from_type == kind, LineageEdge.from_id == source.id,
    ))
    session.add_all([
        LineageEdge(
            tenant_id=tenant_id, incident_id=incident_id,
            from_type=kind, from_id=source.id, to_type="evidence",
            to_id=target_id, relation=RELATIONS[kind],
        ) for target_id in known
    ])


async def rebuild_evidence_page(
    session: AsyncSession, tenant_id: str, incident_id: UUID, kind: str,
    *, cursor: UUID | None = None, limit: int = 20,
) -> dict | None:
    """Repair one bounded page atomically; rerunning a page is idempotent."""
    if kind not in RELATIONS or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid evidence projection page")
    if await session.scalar(select(Incident.id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
    )) is None:
        return None
    model = HypothesisStep if kind == "hypothesis" else IncidentPlan
    stmt = select(model).where(model.incident_id == incident_id)
    if kind == "plan":
        stmt = stmt.where(IncidentPlan.tenant_id == tenant_id)
    if cursor is not None:
        stmt = stmt.where(model.id > cursor)
    rows = (await session.scalars(
        stmt.order_by(model.id).limit(limit + 1).with_for_update()
    )).all()
    visible = rows[:limit]
    for source in visible:
        await replace_evidence_edges(session, tenant_id, incident_id, kind, source)
    await session.flush()
    return {
        "processed": len(visible),
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
    }


async def check_evidence_projection(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    nodes: list[dict], edges: list[dict], *, truncated: bool,
) -> str:
    """Compare only scoped live graph links; never substitute cached authority."""
    if truncated:
        return "NOT_CHECKED_TRUNCATED"
    source_nodes = {node["id"] for node in nodes if node["kind"] in RELATIONS}
    if not source_nodes:
        return "NOT_APPLICABLE"
    expected = {
        (edge["from"], edge["to"]) for edge in edges
        if edge["from"] in source_nodes and edge["relation"] in RELATIONS.values()
    }
    ids = [UUID(node.split(":", 1)[1]) for node in source_nodes]
    rows = (await session.scalars(select(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id, LineageEdge.incident_id == incident_id,
        LineageEdge.from_id.in_(ids),
    ).limit(MAX_EVIDENCE_REFS + 1))).all()
    if len(rows) > MAX_EVIDENCE_REFS:
        return "STALE_OR_MISSING"
    recorded = {
        (f"{row.from_type}:{row.from_id}", f"{row.to_type}:{row.to_id}")
        for row in rows if f"{row.from_type}:{row.from_id}" in source_nodes
    }
    return projection_status(expected, recorded, truncated=False)
