
"""P3: derive bounded causal links from durable tenant-scoped records.

No fabricated telemetry spans; a missing durable foreign reference is explicit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.lineage_refs import MAX_EVIDENCE_REFS, bounded_evidence_refs, reference_uuid
from backend.lineage_projection import check_evidence_projection
from backend.effect_lineage import effect_relationships
from backend.trace_refs import graph_trace_refs
from backend.models import (
    ApprovalGrant, Evidence, EffectReceipt, ExperienceCapsule,
    FailureCase, HypothesisStep, Incident, IncidentPlan,
    OperationalLesson, OutcomeRevision,
)


# Shared authoritative kinds for graph projection and paginated detail reads.
LINEAGE_SOURCE_MODELS = {
    "evidence": Evidence,
    "hypothesis": HypothesisStep,
    "plan": IncidentPlan,
    "effect": EffectReceipt,
    "outcome": OutcomeRevision,
    "experience": ExperienceCapsule,
    "failure": FailureCase,
}


def record_status(kind: str, record: object) -> str:
    if kind == "evidence":
        return str(record.validity)
    if kind == "failure":
        return str(record.review_status)
    return str(getattr(
        record, "state",
        getattr(
            record, "status",
            getattr(record, "label", getattr(record, "decision", "RECORDED")),
        ),
    ))


async def read_lineage_page(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
    kind: str, cursor: UUID | None = None, limit: int = 50,
) -> dict | None:
    """P3 stable keyset page of redacted durable node metadata, never raw evidence."""
    if not 1 <= limit <= 50:
        raise ValueError("lineage page size must be 1–50")
    if kind not in LINEAGE_SOURCE_MODELS and kind not in {"lesson", "approval"}:
        raise ValueError("unsupported lineage record kind")
    exists = await session.scalar(
        select(Incident.id).where(
            Incident.id == incident_id, Incident.tenant_id == tenant_id,
        )
    )
    if exists is None:
        return None
    if kind == "lesson":
        model = OperationalLesson
        stmt = select(model).join(
            ExperienceCapsule, ExperienceCapsule.id == model.source_capsule_id,
        ).where(
            model.tenant_id == tenant_id,
            ExperienceCapsule.tenant_id == tenant_id,
            ExperienceCapsule.incident_id == incident_id,
        )
    elif kind == "approval":
        model = ApprovalGrant
        stmt = select(model).join(
            IncidentPlan, IncidentPlan.id == model.plan_id,
        ).where(
            model.tenant_id == tenant_id,
            IncidentPlan.tenant_id == tenant_id,
            IncidentPlan.incident_id == incident_id,
        )
    else:
        model = LINEAGE_SOURCE_MODELS[kind]
        stmt = select(model).where(model.incident_id == incident_id)
        if hasattr(model, "tenant_id"):
            stmt = stmt.where(model.tenant_id == tenant_id)
    if cursor is not None:
        stmt = stmt.where(model.id > cursor)
    rows = list((await session.scalars(
        stmt.order_by(model.id).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    edges = []
    missing_refs = []
    # Bound fan-out even if persisted JSON references were corrupted.
    references: list[tuple[str, str, UUID, str]] = []
    links_truncated = False
    remaining_evidence = MAX_EVIDENCE_REFS
    direct_kinds = set(LINEAGE_SOURCE_MODELS)
    for row in visible:
        source = f"{kind}:{row.id}"
        if kind in direct_kinds:
            edges.append({
                "from": source, "to": f"incident:{incident_id}",
                "relation": "BELONGS_TO",
            })
        related = []
        if kind in {"hypothesis", "plan"}:
            relation = "CITES" if kind == "hypothesis" else "JUSTIFIED_BY"
            values, omitted, valid = bounded_evidence_refs(row.evidence_ids, remaining_evidence)
            links_truncated |= omitted
            remaining_evidence -= len(values)
            if not valid:
                missing_refs.append({
                    "source": source, "missing_target": "invalid-evidence-list",
                    "relation": relation,
                })
            related = [("evidence", value, relation) for value in values]
        elif kind == "approval":
            related = [("plan", row.plan_id, "REVIEWS")]
        elif kind == "lesson":
            related = [("experience", row.source_capsule_id, "SUPPORTED_BY")]
        elif kind == "outcome" and row.effect_id is not None:
            related = [("effect", row.effect_id, "WITNESSES")]
        elif kind == "experience":
            related = [("outcome", row.outcome_revision_id, "DERIVED_FROM")]
        elif kind == "failure":
            related = [("outcome", row.outcome_revision_id, "CLASSIFIES")]
        for target_kind, value, relation in related:
            if len(references) >= 200:
                links_truncated = True
                break
            target_uuid = reference_uuid(value)
            if target_uuid is None:
                missing_refs.append({
                    "source": source, "missing_target": "invalid-reference",
                    "relation": relation,
                })
                continue
            references.append((source, target_kind, target_uuid, relation))

    models = {
        "evidence": Evidence, "plan": IncidentPlan,
        "effect": EffectReceipt, "outcome": OutcomeRevision,
        "experience": ExperienceCapsule,
    }
    for target_kind in {reference[1] for reference in references}:
        target_model = models[target_kind]
        ids = {ref[2] for ref in references if ref[1] == target_kind}
        query = select(target_model.id).where(
            target_model.id.in_(ids),
            target_model.incident_id == incident_id,
        )
        if hasattr(target_model, "tenant_id"):
            query = query.where(target_model.tenant_id == tenant_id)
        known_ids = set((await session.scalars(query)).all())
        for source, ref_kind, ref_id, relation in references:
            if ref_kind != target_kind:
                continue
            if ref_id in known_ids:
                edges.append({
                    "from": source, "to": f"{ref_kind}:{ref_id}",
                    "relation": relation,
                })
            else:
                missing_refs.append({
                    "source": source, "missing_target": f"{ref_kind}:unavailable",
                    "relation": relation,
                })

    if kind == "effect" and visible:
        digests = {effect.plan_digest for effect in visible}
        plans = (await session.scalars(
            select(IncidentPlan).where(
                IncidentPlan.tenant_id == tenant_id,
                IncidentPlan.incident_id == incident_id,
                IncidentPlan.plan_digest.in_(digests),
            )
        )).all()
        plans_by_digest: dict[str, list[IncidentPlan]] = {}
        for plan in plans:
            plans_by_digest.setdefault(plan.plan_digest, []).append(plan)
        grants = (await session.scalars(
            select(ApprovalGrant).where(
                ApprovalGrant.tenant_id == tenant_id,
                ApprovalGrant.plan_id.in_([plan.id for plan in plans]),
            )
        )).all() if plans else []
        grants_by_plan = {grant.plan_id: grant for grant in grants}
        for effect in visible:
            source = f"effect:{effect.id}"
            matches = plans_by_digest.get(effect.plan_digest, [])
            plan = matches[0] if len(matches) == 1 else None
            grant = grants_by_plan.get(plan.id) if plan is not None else None
            links, gap = effect_relationships(effect, plan, grant)
            for target_kind, target_id, relation in links:
                edges.append({"from": source, "to": f"{target_kind}:{target_id}", "relation": relation})
            if gap is not None:
                missing_refs.append({
                    "source": source,
                    "missing_target": "matching-plan-digest:unavailable" if gap == "REFERENCES_PLAN"
                    else "valid-approval:unavailable",
                    "relation": gap,
                })


    nodes = [
        {"id": f"{kind}:{row.id}", "kind": kind, "status": record_status(kind, row)}
        for row in visible
    ]
    return {
        "incident_id": str(incident_id), "kind": kind,
        "nodes": nodes,
        "trace_refs": await graph_trace_refs(session, tenant_id, incident_id, nodes),
        "evidence_projection": await check_evidence_projection(
            session, tenant_id, incident_id, nodes, edges, truncated=links_truncated,
        ),
        "edges": edges, "missing_refs": missing_refs,
        "links_truncated": links_truncated,
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
        "trace_spans_available": False,
    }


async def build_lineage(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
) -> dict | None:
    incident = await session.scalar(
        select(Incident).where(
            Incident.id == incident_id,
            Incident.tenant_id == tenant_id,
        )
    )
    if incident is None:
        return None

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    missing: list[dict] = []
    truncated_sources: list[str] = []

    def node(kind: str, id_value: UUID, status: str) -> str:
        node_id = f"{kind}:{id_value}"
        nodes[node_id] = {"id": node_id, "kind": kind, "status": status}
        return node_id

    def edge(source: str, target: str, relation: str) -> None:
        if target not in nodes:
            missing.append({
                "source": source, "missing_target": target.split(":", 1)[0] + ":unavailable",
                "relation": relation,
            })
        else:
            edges.append({"from": source, "to": target, "relation": relation})

    root = node("incident", incident.id, incident.status_projection)
    sources = LINEAGE_SOURCE_MODELS.items()
    records: dict[str, list] = {}
    for kind, model in sources:
        query = select(model).where(model.incident_id == incident_id)
        if hasattr(model, "tenant_id"):
            query = query.where(model.tenant_id == tenant_id)
        rows = list((await session.scalars(
            query.order_by(model.id).limit(51)
        )).all())
        if len(rows) > 50:
            truncated_sources.append(kind)
        records[kind] = rows[:50]
        for record in records[kind]:
            node(kind, record.id, record_status(kind, record))

    capsule_ids = [row.id for row in records["experience"]]
    lessons = []
    if capsule_ids:
        lessons = list((await session.scalars(
            select(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.source_capsule_id.in_(capsule_ids),
            ).order_by(OperationalLesson.id).limit(51)
        )).all())
        if len(lessons) > 50:
            truncated_sources.append("lesson")
        lessons = lessons[:50]
    for lesson in lessons:
        node("lesson", lesson.id, lesson.status)

    plan_ids = [row.id for row in records["plan"]]
    grants = []
    if plan_ids:
        grants = list((await session.scalars(
            select(ApprovalGrant).where(
                ApprovalGrant.tenant_id == tenant_id,
                ApprovalGrant.plan_id.in_(plan_ids),
            ).order_by(ApprovalGrant.id).limit(51)
        )).all())
        if len(grants) > 50:
            truncated_sources.append("approval")
        grants = grants[:50]
    for grant in grants:
        node("approval", grant.id, grant.state)
        edge(f"approval:{grant.id}", f"plan:{grant.plan_id}", "REVIEWS")

    for kind in ("evidence", "hypothesis", "plan", "effect", "outcome", "experience", "failure"):
        for record in records[kind]:
            edge(f"{kind}:{record.id}", root, "BELONGS_TO")

    remaining_evidence = MAX_EVIDENCE_REFS
    evidence_links_truncated = False
    for kind, relation in (("hypothesis", "CITES"), ("plan", "JUSTIFIED_BY")):
        for record in records[kind]:
            source = f"{kind}:{record.id}"
            values, omitted, valid = bounded_evidence_refs(
                record.evidence_ids, remaining_evidence,
            )
            remaining_evidence -= len(values)
            evidence_links_truncated |= omitted
            if not valid:
                missing.append({
                    "source": source, "missing_target": "invalid-evidence-list",
                    "relation": relation,
                })
            for value in values:
                evidence_id = reference_uuid(value)
                if evidence_id is None:
                    missing.append({
                        "source": source, "missing_target": "invalid-reference",
                        "relation": relation,
                    })
                else:
                    edge(source, f"evidence:{evidence_id}", relation)

    plans_by_digest: dict[str, list[IncidentPlan]] = {}
    for plan in records["plan"]:
        plans_by_digest.setdefault(plan.plan_digest, []).append(plan)
    grants_by_plan = {grant.plan_id: grant for grant in grants}
    for effect in records["effect"]:
        source = f"effect:{effect.id}"
        matches = plans_by_digest.get(effect.plan_digest, [])
        plan = matches[0] if len(matches) == 1 else None
        grant = grants_by_plan.get(plan.id) if plan is not None else None
        links, gap = effect_relationships(effect, plan, grant)
        for target_kind, target_id, relation in links:
            edge(source, f"{target_kind}:{target_id}", relation)
        if gap is not None:
            missing.append({
                "source": source,
                "missing_target": "matching-plan-digest:unavailable" if gap == "REFERENCES_PLAN"
                else "valid-approval:unavailable",
                "relation": gap,
            })

    for outcome in records["outcome"]:
        if outcome.effect_id is not None:
            edge(
                f"outcome:{outcome.id}",
                f"effect:{outcome.effect_id}",
                "WITNESSES",
            )

    for capsule in records["experience"]:
        edge(
            f"experience:{capsule.id}",
            f"outcome:{capsule.outcome_revision_id}",
            "DERIVED_FROM",
        )

    for lesson in lessons:
        edge(
            f"lesson:{lesson.id}",
            f"experience:{lesson.source_capsule_id}",
            "SUPPORTED_BY",
        )

    for failure in records["failure"]:
        edge(
            f"failure:{failure.id}",
            f"outcome:{failure.outcome_revision_id}",
            "CLASSIFIES",
        )

    return {
        "incident_id": str(incident.id),
        "status_version": incident.status_version,
        "nodes": list(nodes.values()),
        "trace_refs": await graph_trace_refs(session, tenant_id, incident_id, list(nodes.values())),
        "evidence_projection": await check_evidence_projection(
            session, tenant_id, incident_id, list(nodes.values()), edges,
            truncated=bool(truncated_sources or evidence_links_truncated), full_incident=True,
        ),
        "edges": edges,
        "missing_refs": missing,
        "projection": "PARTIAL_LINKS" if missing or truncated_sources or evidence_links_truncated else "COMPLETE",
        "links_truncated": evidence_links_truncated,
        "truncated_sources": truncated_sources,
        "trace_spans_available": False,
    }
