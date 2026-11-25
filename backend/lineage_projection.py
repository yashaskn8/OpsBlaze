"""Transactional causal-edge projection and bounded source-driven repair."""

from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.lineage_projection_refs import EDGE_SPECS, evidence_candidates, projection_status
from backend.effect_lineage import EFFECT_RELATIONS, effect_relationships
from backend.lineage_refs import MAX_EVIDENCE_REFS
from backend.models import (
    ApprovalGrant, EffectReceipt, Evidence, ExperienceCapsule, FailureCase,
    HypothesisStep, Incident, IncidentPlan, LineageEdge, OperationalLesson, OutcomeRevision,
)

SOURCE_MODELS = {
    "hypothesis": HypothesisStep, "plan": IncidentPlan,
    "approval": ApprovalGrant, "outcome": OutcomeRevision, "effect": EffectReceipt,
    "experience": ExperienceCapsule, "lesson": OperationalLesson, "failure": FailureCase,
}
TARGET_MODELS = {
    "evidence": Evidence, "plan": IncidentPlan, "effect": EffectReceipt,
    "outcome": OutcomeRevision, "experience": ExperienceCapsule,
}
DIRECT_REFERENCE_FIELDS = {
    "approval": "plan_id", "outcome": "effect_id", "experience": "outcome_revision_id",
    "lesson": "source_capsule_id", "failure": "outcome_revision_id",
}


def source_query(kind, tenant_id, incident_id):
    model = SOURCE_MODELS[kind]
    stmt = select(model)
    if kind == "approval":
        return stmt.join(IncidentPlan, IncidentPlan.id == ApprovalGrant.plan_id).where(
            ApprovalGrant.tenant_id == tenant_id,
            IncidentPlan.tenant_id == tenant_id, IncidentPlan.incident_id == incident_id,
        )
    if kind == "lesson":
        return stmt.join(ExperienceCapsule, ExperienceCapsule.id == OperationalLesson.source_capsule_id).where(
            OperationalLesson.tenant_id == tenant_id,
            ExperienceCapsule.tenant_id == tenant_id, ExperienceCapsule.incident_id == incident_id,
        )
    stmt = stmt.where(model.incident_id == incident_id)
    if kind != "hypothesis":
        stmt = stmt.where(model.tenant_id == tenant_id)
    return stmt


async def replace_record_edges(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    kind: str, source,
) -> None:
    """Caller owns the transaction; source must be flushed and new or locked."""
    if kind not in SOURCE_MODELS or not isinstance(source, SOURCE_MODELS[kind]):
        raise ValueError("invalid causal projection source")
    if await session.scalar(select(Incident.id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
    )) is None:
        raise ValueError("invalid causal projection scope")
    model = SOURCE_MODELS[kind]
    if await session.scalar(source_query(kind, tenant_id, incident_id).where(
        model.id == source.id,
    )) is None:
        raise ValueError("invalid causal projection source scope")
    if kind == "effect":
        plan = await session.scalar(select(IncidentPlan).where(
            IncidentPlan.tenant_id == tenant_id, IncidentPlan.incident_id == incident_id,
            IncidentPlan.plan_digest == source.plan_digest,
        ))
        grant = await session.scalar(select(ApprovalGrant).where(
            ApprovalGrant.tenant_id == tenant_id, ApprovalGrant.plan_id == plan.id,
        )) if plan is not None else None
        links, _ = effect_relationships(source, plan, grant)
    else:
        target_kind, relation = EDGE_SPECS[kind]
        if kind in {"hypothesis", "plan"}:
            ids = evidence_candidates(source.evidence_ids)
        else:
            target_id = getattr(source, DIRECT_REFERENCE_FIELDS[kind])
            ids = {target_id} if isinstance(target_id, UUID) else set()
        target = TARGET_MODELS[target_kind]
        known = (await session.scalars(select(target.id).where(
            target.id.in_(ids), target.tenant_id == tenant_id,
            target.incident_id == incident_id,
        ))).all() if ids else []
        links = [(target_kind, target_id, relation) for target_id in known]
    await session.execute(delete(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id, LineageEdge.incident_id == incident_id,
        LineageEdge.from_type == kind, LineageEdge.from_id == source.id,
    ))
    session.add_all([
        LineageEdge(
            tenant_id=tenant_id, incident_id=incident_id,
            from_type=kind, from_id=source.id, to_type=target_kind,
            to_id=target_id, relation=relation,
        ) for target_kind, target_id, relation in links
    ])


async def replace_evidence_edges(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    kind: str, source: HypothesisStep | IncidentPlan,
) -> None:
    """Compatibility entrypoint for existing evidence writers."""
    if kind not in {"hypothesis", "plan"}:
        raise ValueError("invalid evidence projection source")
    await replace_record_edges(session, tenant_id, incident_id, kind, source)


async def rebuild_evidence_page(
    session: AsyncSession, tenant_id: str, incident_id: UUID, kind: str,
    *, cursor: UUID | None = None, limit: int = 20,
) -> dict | None:
    """Repair one bounded page atomically; rerunning a page is idempotent."""
    if kind not in SOURCE_MODELS or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid causal projection page")
    if await session.scalar(select(Incident.id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
    )) is None:
        return None
    model = SOURCE_MODELS[kind]
    stmt = source_query(kind, tenant_id, incident_id)
    if cursor is not None:
        stmt = stmt.where(model.id > cursor)
    rows = (await session.scalars(
        stmt.order_by(model.id).limit(limit + 1).with_for_update(of=model)
    )).all()
    visible = rows[:limit]
    for source in visible:
        await replace_record_edges(session, tenant_id, incident_id, kind, source)
    await session.flush()
    return {
        "processed": len(visible),
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
    }


async def prune_orphan_edges(
    session: AsyncSession, tenant_id: str, incident_id: UUID, *, limit: int = 20,
) -> dict | None:
    """Remove only a bounded page of edges whose scoped source no longer exists."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid orphan cleanup page")
    if await session.scalar(select(Incident.id).where(
        Incident.id == incident_id, Incident.tenant_id == tenant_id,
    )) is None:
        return None
    missing = []
    for kind, model in SOURCE_MODELS.items():
        source_exists = source_query(kind, tenant_id, incident_id).where(
            model.id == LineageEdge.from_id,
        ).correlate(LineageEdge).exists()
        missing.append(and_(LineageEdge.from_type == kind, ~source_exists))
    rows = (await session.scalars(select(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id, LineageEdge.incident_id == incident_id,
        or_(*missing),
    ).order_by(
        LineageEdge.from_type, LineageEdge.from_id, LineageEdge.to_id,
    ).limit(limit + 1).with_for_update())).all()
    for row in rows[:limit]:
        await session.delete(row)
    await session.flush()
    return {"removed": min(len(rows), limit), "more": len(rows) > limit}


async def check_evidence_projection(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    nodes: list[dict], edges: list[dict], *, truncated: bool, full_incident: bool = False,
) -> str:
    """Compare scoped live causal links; never substitute cached authority."""
    if truncated:
        return "NOT_CHECKED_TRUNCATED"
    source_nodes = {node["id"] for node in nodes if node["kind"] in SOURCE_MODELS}
    if not source_nodes and not full_incident:
        return "NOT_APPLICABLE"
    relations = {spec[1] for spec in EDGE_SPECS.values()} | EFFECT_RELATIONS
    expected = {
        (edge["from"], edge["to"], edge["relation"]) for edge in edges
        if edge["from"] in source_nodes and edge["relation"] in relations
    }
    ids = [UUID(node.split(":", 1)[1]) for node in source_nodes]
    stmt = select(LineageEdge).where(
        LineageEdge.tenant_id == tenant_id, LineageEdge.incident_id == incident_id,
    )
    if not full_incident:
        stmt = stmt.where(LineageEdge.from_id.in_(ids))
    # Five direct-reference kinds and up to two links per effect, 50 records each.
    cap = MAX_EVIDENCE_REFS + 350
    rows = (await session.scalars(stmt.limit(cap + 1))).all()
    if not source_nodes and not rows:
        return "NOT_APPLICABLE"
    if len(rows) > cap:
        return "STALE_OR_MISSING"
    recorded = {
        (f"{row.from_type}:{row.from_id}", f"{row.to_type}:{row.to_id}", row.relation)
        for row in rows if full_incident or f"{row.from_type}:{row.from_id}" in source_nodes
    }
    return projection_status(expected, recorded, truncated=False)
