
"""P3: derive bounded causal links from durable tenant-scoped records.

No fabricated telemetry spans; a missing durable foreign reference is explicit.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.adapters.simulator import utc
from backend.lineage_refs import MAX_EVIDENCE_REFS, bounded_evidence_refs, reference_uuid
from backend.lineage_projection import check_evidence_projection
from backend.effect_lineage import effect_relationships
from backend.trace_refs import graph_trace_refs
from backend.models import (
    ApprovalGrant, Evidence, EffectReceipt, ExperienceCapsule,
    FailureCase, HypothesisStep, Incident, IncidentPlan,
    LineageRecoveryCheckpoint, OperationalLesson, OutcomeRevision,
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

ALL_LINEAGE_KINDS = frozenset(LINEAGE_SOURCE_MODELS) | {"lesson", "approval"}

AUTHORITY_KINDS = {
    "evidence": "OBSERVED_EVIDENCE",
    "hypothesis": "MODEL_OR_INVESTIGATION_CLAIM",
    "plan": "PROPOSED_ACTION",
    "approval": "HUMAN_AUTHORIZATION",
    "effect": "EFFECT_LEDGER",
    "outcome": "INDEPENDENT_OUTCOME",
    "experience": "DERIVED_LEARNING",
    "lesson": "DERIVED_LEARNING",
    "failure": "DERIVED_LEARNING",
}


def _iso(value: datetime | None) -> str | None:
    return utc(value).isoformat() if value is not None else None


def _is_unresolved_or_failed(kind: str, status: str) -> bool:
    if kind == "effect":
        return status in {"UNKNOWN", "FAILED", "NEEDS_REVIEW", "SENT", "CONFIRMED"}
    if kind == "outcome":
        return status in {"FAILED", "INCONCLUSIVE", "DISPUTED"}
    if kind in {"approval", "plan"}:
        return status in {"REJECTED", "REVOKED"}
    if kind == "hypothesis":
        return status in {"ABSTAINED", "FAILED"}
    if kind == "evidence":
        return status != "VALID"
    if kind in {"experience", "lesson"}:
        return status in {"REVOKED", "INVALIDATED", "EXPIRED", "QUARANTINED"}
    if kind == "failure":
        return status != "APPROVED"
    return False


async def _lineage_diagnostics(
    session: AsyncSession,
    nodes: list[dict],
    trace_refs: list[dict],
    missing_refs: list[dict],
    evidence_projection: str,
) -> dict:
    recorded_nodes = {ref["node_id"] for ref in trace_refs}
    trace_eligible = [
        node["id"] for node in nodes if node["kind"] in {"incident", "outcome"}
    ]
    context_loss_count = sum(1 for node_id in trace_eligible if node_id not in recorded_nodes)
    projection_lag_seconds: int | None
    if evidence_projection in {"MATCHED", "NOT_APPLICABLE"}:
        projection_lag_seconds = 0
    else:
        checkpoint = await session.get(LineageRecoveryCheckpoint, 1)
        if checkpoint is not None and checkpoint.completed_at is not None:
            now = datetime.now(timezone.utc)
            projection_lag_seconds = max(
                0, int((now - utc(checkpoint.completed_at)).total_seconds()),
            )
        else:
            projection_lag_seconds = None
    return {
        "missing_edge_count": len(missing_refs),
        "context_loss_count": context_loss_count,
        "projection_lag_seconds": projection_lag_seconds,
        "projection_status": evidence_projection,
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
    for row in visible:
        source = f"{kind}:{row.id}"
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
    trace_refs = await graph_trace_refs(session, tenant_id, incident_id, nodes)
    evidence_projection = await check_evidence_projection(
        session, tenant_id, incident_id, nodes, edges, truncated=links_truncated,
    )
    return {
        "incident_id": str(incident_id), "kind": kind,
        "nodes": nodes,
        "trace_refs": trace_refs,
        "evidence_projection": evidence_projection,
        "diagnostics": await _lineage_diagnostics(
            session, nodes, trace_refs, missing_refs, evidence_projection,
        ),
        "edges": edges, "missing_refs": missing_refs,
        "links_truncated": links_truncated,
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
        "trace_spans_available": False,
    }


async def read_lineage_record_detail(
    session: AsyncSession,
    tenant_id: str,
    incident_id: UUID,
    kind: str,
    record_id: UUID,
) -> dict | None:
    """Authoritative tenant-scoped detail for a single lineage node."""
    if kind not in ALL_LINEAGE_KINDS:
        raise ValueError("unsupported lineage record kind")
    exists = await session.scalar(
        select(Incident.id).where(
            Incident.id == incident_id, Incident.tenant_id == tenant_id,
        )
    )
    if exists is None:
        return None
    if kind == "lesson":
        row = await session.scalar(
            select(OperationalLesson)
            .join(
                ExperienceCapsule,
                ExperienceCapsule.id == OperationalLesson.source_capsule_id,
            )
            .where(
                OperationalLesson.id == record_id,
                OperationalLesson.tenant_id == tenant_id,
                ExperienceCapsule.tenant_id == tenant_id,
                ExperienceCapsule.incident_id == incident_id,
            )
        )
    elif kind == "approval":
        row = await session.scalar(
            select(ApprovalGrant)
            .join(IncidentPlan, IncidentPlan.id == ApprovalGrant.plan_id)
            .where(
                ApprovalGrant.id == record_id,
                ApprovalGrant.tenant_id == tenant_id,
                IncidentPlan.tenant_id == tenant_id,
                IncidentPlan.incident_id == incident_id,
            )
        )
    else:
        model = LINEAGE_SOURCE_MODELS[kind]
        stmt = select(model).where(
            model.id == record_id,
            model.incident_id == incident_id,
        )
        if hasattr(model, "tenant_id"):
            stmt = stmt.where(model.tenant_id == tenant_id)
        row = await session.scalar(stmt)
    if row is None:
        return None

    status = record_status(kind, row)
    if kind == "evidence":
        details = {
            "source": row.source,
            "source_version": row.source_version,
            "target_id": row.target_id,
            "target_version": row.target_version,
            "validity": row.validity,
            "classification": row.classification,
            "observed_at": _iso(row.observed_at),
            "sha256": row.sha256,
            "redacted_excerpt": row.redacted_excerpt,
        }
    elif kind == "hypothesis":
        details = {
            "step_no": row.step_no,
            "model_id": row.model_id,
            "prompt_version": row.prompt_version,
            "probe_id": row.probe_id,
            "decision": row.decision,
            "cause_scores": row.cause_scores,
            "evidence_count": len(row.evidence_ids) if isinstance(row.evidence_ids, list) else 0,
            "claim_only": True,
            "verified_recovery": False,
        }
    elif kind == "plan":
        details = {
            "action_id": row.action_id,
            "target_id": row.target_id,
            "expected_version": row.expected_version,
            "status": row.status,
            "version": row.version,
            "plan_digest": row.plan_digest,
            "evidence_count": len(row.evidence_ids) if isinstance(row.evidence_ids, list) else 0,
            "created_at": _iso(row.created_at),
            "decided_at": _iso(row.decided_at),
        }
    elif kind == "approval":
        details = {
            "plan_id": str(row.plan_id),
            "plan_digest": row.plan_digest,
            "decision": row.decision,
            "state": row.state,
            "scope": row.scope,
            "version": row.version,
            "reviewer_id": row.reviewer_id,
            "reason": row.reason,
            "expires_at": _iso(row.expires_at),
            "revoked_at": _iso(row.revoked_at),
            "consumed_at": _iso(row.consumed_at),
            "created_at": _iso(row.created_at),
        }
    elif kind == "effect":
        details = {
            "adapter_id": row.adapter_id,
            "target_id": row.target_id,
            "state": row.state,
            "expected_version": row.expected_version,
            "observed_version": row.observed_version,
            "attempt_count": row.attempt_count,
            "fence": row.fence,
            "plan_digest": row.plan_digest,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
    elif kind == "outcome":
        details = {
            "revision": row.revision,
            "label": row.label,
            "witness_source": row.witness_source,
            "observed_version": row.observed_version,
            "effect_id": str(row.effect_id) if row.effect_id is not None else None,
            "reason": row.reason,
            "reviewer_id": row.reviewer_id,
            "recorded_at": _iso(row.created_at),
        }
    elif kind == "experience":
        details = {
            "outcome_revision_id": str(row.outcome_revision_id),
            "status": row.status,
            "consent_epoch": row.consent_epoch,
            "tenant_consent_epoch": row.tenant_consent_epoch,
            "target_digest": row.target_digest,
            "evidence_count": len(row.evidence_ids) if isinstance(row.evidence_ids, list) else 0,
            "created_at": _iso(row.created_at),
            "expires_at": _iso(row.expires_at),
        }
    elif kind == "lesson":
        details = {
            "source_capsule_id": str(row.source_capsule_id),
            "source_outcome_id": str(row.source_outcome_id),
            "rule_id": row.rule_id,
            "summary_redacted": row.summary_redacted,
            "status": row.status,
            "version": row.version,
            "valid_from": _iso(row.valid_from),
            "valid_until": _iso(row.valid_until),
        }
    else:
        details = {
            "outcome_revision_id": str(row.outcome_revision_id),
            "cluster_id": str(row.cluster_id),
            "origin_code": row.origin_code,
            "first_bad_step": row.first_bad_step,
            "classification_version": row.classification_version,
            "review_status": row.review_status,
            "created_at": _iso(row.created_at),
        }

    node_id = f"{kind}:{row.id}"
    return {
        "id": node_id,
        "kind": kind,
        "record_id": str(row.id),
        "incident_id": str(incident_id),
        "status": status,
        "authority_kind": AUTHORITY_KINDS[kind],
        "verified_outcome": kind == "outcome" and row.label == "VERIFIED",
        "unresolved_or_failed": _is_unresolved_or_failed(kind, status),
        "details": details,
        "trace_refs": await graph_trace_refs(
            session, tenant_id, incident_id, [{"id": node_id, "kind": kind}],
        ),
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
        edge(f"lesson:{lesson.id}", root, "BELONGS_TO")

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
        edge(f"approval:{grant.id}", root, "BELONGS_TO")
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

    node_list = list(nodes.values())
    trace_refs = await graph_trace_refs(session, tenant_id, incident_id, node_list)
    evidence_projection = await check_evidence_projection(
        session, tenant_id, incident_id, node_list, edges,
        truncated=bool(truncated_sources or evidence_links_truncated), full_incident=True,
    )
    return {
        "incident_id": str(incident.id),
        "status_version": incident.status_version,
        "nodes": node_list,
        "trace_refs": trace_refs,
        "evidence_projection": evidence_projection,
        "diagnostics": await _lineage_diagnostics(
            session, node_list, trace_refs, missing, evidence_projection,
        ),
        "edges": edges,
        "missing_refs": missing,
        "projection": "PARTIAL_LINKS" if missing or truncated_sources or evidence_links_truncated else "COMPLETE",
        "links_truncated": evidence_links_truncated,
        "truncated_sources": truncated_sources,
        "trace_spans_available": False,
    }
