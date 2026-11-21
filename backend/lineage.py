
"""P3: derive bounded causal links from durable tenant-scoped records.

No fabricated telemetry spans; a missing durable foreign reference is explicit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


def matching_effect_plan(
    effect: EffectReceipt, plan: IncidentPlan | None,
) -> bool:
    return (
        plan is not None
        and plan.incident_id == effect.incident_id
        and plan.target_id == effect.target_id
        and plan.expected_version == effect.expected_version
        and plan.action_id == "SIMULATOR_MARK_HEALTHY"
        and effect.adapter_id == "simulator"
    )


def consumed_effect_authority(
    effect: EffectReceipt, plan: IncidentPlan, grant: ApprovalGrant | None,
) -> bool:
    return (
        effect.attempt_count == 1
        and effect.state != "PREPARED"
        and grant is not None
        and grant.plan_digest == plan.plan_digest
        and grant.scope == plan.action_id
        and grant.decision == "APPROVE"
        and grant.state == "CONSUMED"
        and plan.status == "APPROVED"
        and plan.decided_by == grant.reviewer_id
    )


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
    return {
        "incident_id": str(incident_id), "kind": kind,
        "nodes": [
            {"id": f"{kind}:{row.id}", "kind": kind,
             "status": record_status(kind, row)}
            for row in visible
        ],
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
                "source": source, "missing_target": target, "relation": relation,
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
    grants_by_plan = {grant.plan_id: grant for grant in grants}
    for effect in records["effect"]:
        source = f"effect:{effect.id}"
        plan = plans_by_digest.get(effect.plan_digest)
        if not matching_effect_plan(effect, plan):
            missing.append({
                "source": source,
                "missing_target": f"matching-plan-digest:{effect.plan_digest}",
                "relation": "REFERENCES_PLAN",
            })
            continue
        if effect.state == "PREPARED" and effect.attempt_count == 0:
            edge(source, f"plan:{plan.id}", "PREPARED_FOR")
            continue
        grant = grants_by_plan.get(plan.id)
        if not consumed_effect_authority(effect, plan, grant):
            # The digest still links a forensic plan reference, but that
            # reference must not be displayed as valid effect authority.
            edge(source, f"plan:{plan.id}", "REFERENCES_PLAN")
            missing.append({
                "source": source,
                "missing_target": f"valid-approval-for-plan:{plan.id}",
                "relation": "AUTHORIZED_BY_GRANT",
            })
            continue
        edge(source, f"plan:{plan.id}", "AUTHORIZED_BY")
        edge(source, f"approval:{grant.id}", "AUTHORIZED_BY_GRANT")

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
        "projection": "PARTIAL_LINKS" if missing or truncated_sources else "COMPLETE",
        "truncated_sources": truncated_sources,
        "trace_spans_available": False,
    }
