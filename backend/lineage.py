
"""P3: derive bounded causal links from durable tenant-scoped records.

No fabricated telemetry spans; a missing durable foreign reference is explicit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Evidence, EffectReceipt, ExperienceCapsule, FailureCase,
    HypothesisStep, Incident, IncidentPlan, OperationalLesson,
    OutcomeRevision,
)


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
    refs: dict[str, set[str]] = {}

    def node(kind: str, id_value: UUID, status: str) -> str:
        node_id = f"{kind}:{id_value}"
        nodes[node_id] = {"id": node_id, "kind": kind, "status": status}
        refs.setdefault(kind, set()).add(str(id_value))
        return node_id

    def edge(source: str, target: str, relation: str) -> None:
        if target not in nodes:
            missing.append({
                "source": source, "missing_target": target, "relation": relation,
            })
        else:
            edges.append({"from": source, "to": target, "relation": relation})

    root = node("incident", incident.id, incident.status_projection)
    sources = (
        ("evidence", Evidence),
        ("hypothesis", HypothesisStep),
        ("plan", IncidentPlan),
        ("effect", EffectReceipt),
        ("outcome", OutcomeRevision),
        ("experience", ExperienceCapsule),
        ("failure", FailureCase),
    )
    records: dict[str, list] = {}
    for kind, model in sources:
        rows = (await session.scalars(
            select(model).where(
                model.incident_id == incident_id,
                model.tenant_id == tenant_id,
            ).limit(50)
        )).all() if hasattr(model, "tenant_id") else (await session.scalars(
            select(model).where(model.incident_id == incident_id).limit(50)
        )).all()
        records[kind] = list(rows)
        for record in rows:
            state = getattr(
                record, "state",
                getattr(
                    record, "status",
                    getattr(record, "label", getattr(record, "decision", "RECORDED")),
                ),
            )
            if kind == "evidence":
                state = record.validity
            if kind == "failure":
                state = record.review_status
            node(kind, record.id, str(state))

    capsule_ids = [row.id for row in records["experience"]]
    lessons = []
    if capsule_ids:
        lessons = (await session.scalars(
            select(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.source_capsule_id.in_(capsule_ids),
            ).limit(50)
        )).all()
    for lesson in lessons:
        node("lesson", lesson.id, lesson.status)

    for kind in ("evidence", "hypothesis", "plan", "effect", "outcome", "experience", "failure"):
        for record in records[kind]:
            edge(f"{kind}:{record.id}", root, "BELONGS_TO")

    for step in records["hypothesis"]:
        for evidence_id in step.evidence_ids:
            edge(
                f"hypothesis:{step.id}", f"evidence:{evidence_id}", "CITES",
            )

    for plan in records["plan"]:
        for evidence_id in plan.evidence_ids:
            edge(f"plan:{plan.id}", f"evidence:{evidence_id}", "JUSTIFIED_BY")

    plans_by_digest = {
        plan.plan_digest: plan for plan in records["plan"]
    }
    for effect in records["effect"]:
        plan = plans_by_digest.get(effect.plan_digest)
        if plan is None:
            missing.append({
                "source": f"effect:{effect.id}",
                "missing_target": f"plan-digest:{effect.plan_digest}",
                "relation": "AUTHORIZED_BY",
            })
        else:
            edge(f"effect:{effect.id}", f"plan:{plan.id}", "AUTHORIZED_BY")

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
        "edges": edges,
        "missing_refs": missing,
        "projection": "PARTIAL_LINKS" if missing else "COMPLETE",
        "trace_spans_available": False,
    }
