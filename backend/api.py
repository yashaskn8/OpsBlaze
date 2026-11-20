import re
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.schemas import IncidentCreate, IncidentView
from backend.services import IdempotencyConflict, RateLimited, admit_incident, find_incident
from backend.settings import Settings, get_settings

app = FastAPI(title="OpsBlaze", version="0.1.0")


def local_tenant(
    x_tenant_id: Annotated[str, Header(min_length=1, max_length=64)],
    x_opsblaze_token: Annotated[str, Header()],
    settings: Settings = Depends(get_settings),
) -> str:
    # Local-only bootstrap authentication. Do not deploy as multi-tenant auth.
    if settings.environment != "development" or not settings.local_token:
        raise HTTPException(503, "local API disabled; production auth not implemented")
    if not secrets.compare_digest(x_opsblaze_token, settings.local_token):
        raise HTTPException(401, "invalid local API token")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", x_tenant_id) is None:
        raise HTTPException(422, "invalid tenant identifier")
    return x_tenant_id


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/incidents", response_model=IncidentView, status_code=202)
async def create_incident(
    payload: IncidentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    if not idempotency_key.strip():
        raise HTTPException(422, "idempotency key cannot be blank")
    try:
        incident = await admit_incident(session, tenant_id, idempotency_key, payload)
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    return IncidentView.model_validate(incident)


@app.get("/v1/incidents/{incident_id}", response_model=IncidentView)
async def get_incident(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/cancel", response_model=IncidentView, status_code=202)
async def cancel_incident(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    from backend.services import VersionConflict, request_cancellation

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the status version")
    try:
        incident = await request_cancellation(session, tenant_id, incident_id, int(version))
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/acknowledge", response_model=IncidentView, status_code=202)
async def acknowledge_incident_review(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    from backend.services import VersionConflict, acknowledge_review

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the status version")
    try:
        incident = await acknowledge_review(session, tenant_id, incident_id, int(version))
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)


@app.get("/v1/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    from sqlalchemy import text
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}


@app.get("/v1/operations/queues")
async def tenant_queues(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    from sqlalchemy import func, select
    from backend.models import Incident, OutboxEntry

    pending = await session.scalar(
        select(func.count())
        .select_from(OutboxEntry)
        .join(Incident, Incident.id == OutboxEntry.incident_id)
        .where(Incident.tenant_id == tenant_id, OutboxEntry.delivered_at.is_(None))
    )
    active = await session.scalar(
        select(func.count())
        .select_from(Incident)
        .where(
            Incident.tenant_id == tenant_id,
            Incident.status_projection.in_(
                ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
                 "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED"]
            ),
        )
    )
    return {"pending_outbox": pending or 0, "active_incidents": active or 0}


@app.get("/v1/incidents/{incident_id}/evidence")
async def incident_evidence(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import Evidence

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(Evidence)
        .where(Evidence.incident_id == incident_id, Evidence.tenant_id == tenant_id)
        .order_by(Evidence.captured_at, Evidence.id)
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "source": row.source,
         "target_version": row.target_version, "sha256": row.sha256,
         "validity": row.validity, "redacted_excerpt": row.redacted_excerpt,
         "captured_at": row.captured_at.isoformat()}
        for row in items
    ]


@app.get("/v1/incidents/{incident_id}/hypotheses")
async def incident_hypotheses(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import HypothesisStep

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(HypothesisStep)
        .where(HypothesisStep.incident_id == incident_id)
        .order_by(HypothesisStep.step_no)
        .limit(50)
    )).all()
    return [
        {"step_no": row.step_no, "decision": row.decision,
         "probe_id": row.probe_id, "model_id": row.model_id,
         "evidence_ids": row.evidence_ids, "cause_scores": row.cause_scores}
        for row in items
    ]


@app.get("/v1/incidents/{incident_id}/effects")
async def incident_effects(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import EffectReceipt

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(EffectReceipt)
        .where(EffectReceipt.tenant_id == tenant_id,
               EffectReceipt.incident_id == incident_id)
        .order_by(EffectReceipt.created_at, EffectReceipt.id)
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "state": row.state, "adapter_id": row.adapter_id,
         "target_id": row.target_id, "fence": row.fence,
         "observed_version": row.observed_version}
        for row in items
    ]


from typing import Literal
from pydantic import BaseModel, ConfigDict


class PlanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]


@app.get("/v1/incidents/{incident_id}/plans")
async def incident_plans(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import IncidentPlan

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    plans = (await session.scalars(
        select(IncidentPlan)
        .where(IncidentPlan.incident_id == incident_id, IncidentPlan.tenant_id == tenant_id)
        .limit(10)
    )).all()
    return [
        {"id": str(plan.id), "action_id": plan.action_id,
         "target_id": plan.target_id, "status": plan.status,
         "version": plan.version, "plan_digest": plan.plan_digest,
         "expected_version": plan.expected_version,
         "evidence_ids": plan.evidence_ids}
        for plan in plans
    ]


@app.post("/v1/plans/{plan_id}/decisions", status_code=201)
async def decide_incident_plan(
    plan_id: UUID,
    payload: PlanDecisionRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.plans import PlanConflict, decide_plan

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the plan version")
    try:
        plan = await decide_plan(
            session, tenant_id, plan_id, int(version), payload.decision
        )
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if plan is None:
        raise HTTPException(404, "plan not found")
    return {"id": str(plan.id), "status": plan.status, "version": plan.version}


@app.get("/v1/targets/{target_id}/simulator")
async def simulator_target_view(
    target_id: str,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import SimulatorTarget

    target = await session.get(SimulatorTarget, (tenant_id, target_id))
    if target is None:
        raise HTTPException(404, "simulator target not found")
    return {"target_id": target.target_id, "version": target.version,
            "healthy": target.healthy, "adapter": "simulator"}


from pydantic import Field


class OutcomeFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=256)


@app.get("/v1/incidents/{incident_id}/outcomes")
async def incident_outcomes(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import OutcomeRevision

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = (await session.scalars(
        select(OutcomeRevision)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == incident_id,
        )
        .order_by(OutcomeRevision.revision)
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "revision": row.revision,
         "label": row.label, "witness_source": row.witness_source,
         "observed_version": row.observed_version, "reason": row.reason,
         "created_at": row.created_at.isoformat()}
        for row in rows
    ]


@app.post("/v1/outcomes/{outcome_id}/feedback", status_code=202)
async def report_outcome_dispute(
    outcome_id: UUID,
    payload: OutcomeFeedback,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.outcomes import OutcomeConflict, dispute_outcome

    revision = if_match.strip('"')
    if not revision.isdecimal():
        raise HTTPException(400, "If-Match must be the outcome revision")
    try:
        row = await dispute_outcome(
            session, tenant_id, outcome_id, int(revision), payload.reason
        )
    except OutcomeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "outcome not found")
    return {"id": str(row.id), "revision": row.revision, "label": row.label}


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool


@app.post("/v1/incidents/{incident_id}/consent", status_code=202)
async def update_experience_consent(
    incident_id: UUID,
    payload: ConsentRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiences import ConsentConflict, set_consent

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the consent epoch")
    try:
        consent = await set_consent(
            session, tenant_id, incident_id, int(version), payload.allow
        )
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if consent is None:
        raise HTTPException(404, "incident not found")
    return {"incident_id": str(incident_id), "epoch": consent.epoch,
            "allowed": consent.allowed}


@app.post("/v1/incidents/{incident_id}/experiences/capture", status_code=201)
async def capture_incident_experience(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiences import ConsentConflict, capture_capsule

    try:
        capsule = await capture_capsule(session, tenant_id, incident_id)
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if capsule is None:
        raise HTTPException(404, "incident not found")
    return {"id": str(capsule.id), "status": capsule.status,
            "consent_epoch": capsule.consent_epoch}


@app.get("/v1/experiences")
async def tenant_experiences(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from backend.experiences import list_capsules

    rows = await list_capsules(session, tenant_id)
    return [
        {"id": str(row.id), "incident_id": str(row.incident_id),
         "outcome_revision_id": str(row.outcome_revision_id),
         "consent_epoch": row.consent_epoch,
         "target_digest": row.target_digest,
         "evidence_ids": row.evidence_ids,
         "expires_at": row.expires_at.isoformat()}
        for row in rows
    ]


class ImprovementDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_cluster_id: UUID
    kind: Literal["PROBE_ORDER", "CONTEXT_LIMIT", "ROUTE_SWITCH"]
    base_policy_id: str = Field(min_length=1, max_length=64)
    change_spec: dict
    hypothesis: str = Field(min_length=8, max_length=512)
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    evaluation_manifest_id: str = Field(min_length=1, max_length=64)


@app.post("/v1/failures/classify/{outcome_id}", status_code=201)
async def classify_failure(
    outcome_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import FailureConflict, record_case

    try:
        case = await record_case(session, tenant_id, outcome_id)
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "outcome not found")
    return {
        "id": str(case.id), "cluster_id": str(case.cluster_id),
        "origin_code": case.origin_code, "review_status": case.review_status,
        "classification_version": case.classification_version,
    }


@app.get("/v1/failures")
async def list_failure_cases(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import FailureCase

    cases = (await session.scalars(
        select(FailureCase).where(FailureCase.tenant_id == tenant_id)
        .order_by(FailureCase.created_at.desc(), FailureCase.id)
        .limit(50)
    )).all()
    return [
        {"id": str(case.id), "incident_id": str(case.incident_id),
         "outcome_revision_id": str(case.outcome_revision_id),
         "cluster_id": str(case.cluster_id), "origin_code": case.origin_code,
         "review_status": case.review_status,
         "classification_version": case.classification_version}
        for case in cases
    ]


@app.get("/v1/failure-clusters/{cluster_id}")
async def failure_cluster_view(
    cluster_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import FailureCase, FailureCluster

    cluster = await session.scalar(
        select(FailureCluster).where(
            FailureCluster.id == cluster_id, FailureCluster.tenant_id == tenant_id
        )
    )
    if cluster is None:
        raise HTTPException(404, "cluster not found")
    cases = (await session.scalars(
        select(FailureCase).where(
            FailureCase.cluster_id == cluster.id,
            FailureCase.tenant_id == tenant_id,
        ).order_by(FailureCase.created_at).limit(50)
    )).all()
    return {"id": str(cluster.id), "signature": cluster.signature,
            "version": cluster.version, "case_count": cluster.case_count,
            "quarantined": cluster.quarantined,
            "source_incidents": [str(case.incident_id) for case in cases]}


@app.post("/v1/improvements/draft", status_code=201)
async def create_improvement_draft(
    payload: ImprovementDraft,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import FailureConflict, draft_candidate

    try:
        row = await draft_candidate(
            session, tenant_id, payload.source_cluster_id, payload.kind,
            payload.base_policy_id, payload.change_spec,
            payload.hypothesis, payload.risk, payload.evaluation_manifest_id,
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(row.id), "state": row.state,
            "version": row.version, "kind": row.kind}


@app.post("/v1/improvements/{candidate_id}/submit", status_code=202)
async def submit_improvement(
    candidate_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import FailureConflict, submit_candidate

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the candidate version")
    try:
        candidate = await submit_candidate(
            session, tenant_id, candidate_id, int(version)
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    return {"id": str(candidate.id), "state": candidate.state,
            "version": candidate.version}


@app.get("/v1/improvements")
async def list_improvements(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import ImprovementCandidate

    rows = (await session.scalars(
        select(ImprovementCandidate)
        .where(ImprovementCandidate.tenant_id == tenant_id)
        .order_by(ImprovementCandidate.created_at.desc())
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "source_cluster_id": str(row.source_cluster_id),
         "kind": row.kind, "base_policy_id": row.base_policy_id,
         "change_spec": row.change_spec, "hypothesis": row.hypothesis,
         "risk": row.risk, "state": row.state, "version": row.version,
         "evaluation_manifest_id": row.evaluation_manifest_id}
        for row in rows
    ]


class LessonDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_capsule_id: UUID
    rule_id: Literal[
        "PROBE_HEALTH_FIRST", "CHECK_VERSION", "ESCALATE_UNCERTAIN"
    ]


@app.post("/v1/lessons", status_code=201)
async def create_lesson_draft(
    payload: LessonDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.memory.lessons import LessonConflict, create_draft

    try:
        lesson = await create_draft(
            session, tenant_id, payload.source_capsule_id, payload.rule_id
        )
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version, "rule_id": lesson.rule_id}


@app.post("/v1/lessons/{lesson_id}/validate", status_code=202)
async def validate_lesson(
    lesson_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.memory.lessons import LessonConflict, validate_draft

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the lesson version")
    try:
        lesson = await validate_draft(session, tenant_id, lesson_id, int(version))
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if lesson is None:
        raise HTTPException(404, "lesson not found")
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version}


@app.post("/v1/lessons/{lesson_id}/revoke", status_code=202)
async def revoke_operational_lesson(
    lesson_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.memory.lessons import LessonConflict, revoke_lesson

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the lesson version")
    try:
        lesson = await revoke_lesson(session, tenant_id, lesson_id, int(version))
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if lesson is None:
        raise HTTPException(404, "lesson not found")
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version}


@app.get("/v1/lessons")
async def visible_lessons(
    target_id: str,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from backend.memory.lessons import retrieve_active_lessons

    rows = await retrieve_active_lessons(session, tenant_id, target_id)
    return [
        {"id": str(row.id), "rule_id": row.rule_id,
         "summary_redacted": row.summary_redacted,
         "source_outcome_id": str(row.source_outcome_id),
         "version": row.version}
        for row in rows
    ]
