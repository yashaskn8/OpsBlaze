from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import Principal, get_principal, local_operator, local_reviewer, local_tenant
from backend.database import get_session
from backend.schemas import IncidentCreate, IncidentView
from backend.services import IdempotencyConflict, RateLimited, admit_incident, find_incident
from backend.settings import Settings, get_settings

app = FastAPI(title="OpsBlaze", version="0.1.0")


@app.get("/v1/auth/me")
async def authenticated_identity(
    principal: Principal = Depends(get_principal),
) -> dict:
    # Never return an email, raw sub, bearer token, or unverified header.
    return {
        "tenant_id": principal.tenant_id,
        "actor_id": principal.subject,
        "roles": sorted(principal.roles),
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/incidents", response_model=IncidentView, status_code=202)
async def create_incident(
    payload: IncidentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> IncidentView:
    if not idempotency_key.strip():
        raise HTTPException(422, "idempotency key cannot be blank")
    try:
        incident = await admit_incident(
            session, tenant_id, idempotency_key, payload,
            created_by=operator_identity,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    return IncidentView.model_validate(incident)


@app.get("/v1/incidents/{incident_id}", response_model=IncidentView)
async def get_incident(
    incident_id: UUID,
    response: Response,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    response.headers["ETag"] = f'"{incident.status_version}"'
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/cancel", response_model=IncidentView, status_code=202)
async def cancel_incident(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
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
    operator_identity: str = Depends(local_operator),
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
async def readiness(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    from sqlalchemy import text
    from backend.workers.heartbeat import REQUIRED_ROLES, present_workers

    try:
        await session.execute(text("SELECT 1"))
        if settings.environment == "production":
            present = await present_workers(
                session,
                max_age_seconds=settings.worker_stale_seconds,
            )
            if not REQUIRED_ROLES.issubset(present):
                raise HTTPException(503, "required worker heartbeat missing or stale")
    except HTTPException:
        raise
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
    from backend.services import ACTIVE_STATES

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
            Incident.status_projection.in_(ACTIVE_STATES),
        )
    )
    from datetime import datetime, timezone
    from backend.adapters.simulator import utc

    oldest_pending = await session.scalar(
        select(func.min(OutboxEntry.created_at))
        .join(Incident, Incident.id == OutboxEntry.incident_id)
        .where(
            Incident.tenant_id == tenant_id,
            OutboxEntry.delivered_at.is_(None),
        )
    )
    oldest_age = (
        max(0, int((datetime.now(timezone.utc) - utc(oldest_pending)).total_seconds()))
        if oldest_pending is not None else 0
    )
    return {
        "pending_outbox": pending or 0,
        "active_incidents": active or 0,
        "oldest_pending_seconds": oldest_age,
    }


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
from pydantic import BaseModel, ConfigDict, Field


class PlanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(default="simulator plan reviewed", min_length=8, max_length=256)


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
    reviewer_identity: str = Depends(local_reviewer),
    settings: Settings = Depends(get_settings),
) -> dict:
    from backend.plans import PlanConflict, decide_plan

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the plan version")
    try:
        plan = await decide_plan(
            session, tenant_id, plan_id, int(version), payload.decision,
            decided_by=reviewer_identity,
            review_epoch=settings.review_epoch,
            reason=payload.reason,
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
    reviewer_identity: str = Depends(local_reviewer),
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
    operator_identity: str = Depends(local_operator),
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
    operator_identity: str = Depends(local_operator),
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
    operator_identity: str = Depends(local_operator),
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
    operator_identity: str = Depends(local_operator),
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
    reviewer_identity: str = Depends(local_reviewer),
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
    operator_identity: str = Depends(local_operator),
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
    reviewer_identity: str = Depends(local_reviewer),
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
    reviewer_identity: str = Depends(local_reviewer),
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


class EvaluationRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID


@app.post("/v1/evaluations", status_code=201)
async def register_evaluation(
    payload: EvaluationRegistration,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.evaluation.smoke import EvaluationConflict, freeze
    from backend.learning.failures import FailureConflict

    try:
        manifest = await freeze(session, tenant_id, payload.candidate_id)
    except (EvaluationConflict, FailureConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(manifest.id), "state": manifest.state,
            "fixture_digest": manifest.fixture_digest}


@app.post("/v1/evaluations/{manifest_id}/run", status_code=202)
async def run_evaluation(
    manifest_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.evaluation.smoke import EvaluationConflict, run_smoke

    try:
        manifest = await run_smoke(session, tenant_id, manifest_id)
    except EvaluationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    return {"id": str(manifest.id), "state": manifest.state,
            "promotion_eligible": False}


@app.get("/v1/evaluations/{manifest_id}")
async def get_evaluation(
    manifest_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.evaluation.smoke import report

    result = await report(session, tenant_id, manifest_id)
    if result is None:
        raise HTTPException(404, "manifest not found")
    return result


@app.get("/v1/incidents/{incident_id}/lineage")
async def incident_lineage(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.lineage import build_lineage

    lineage = await build_lineage(session, tenant_id, incident_id)
    if lineage is None:
        raise HTTPException(404, "incident not found")
    return lineage


@app.get("/v1/incidents/{incident_id}/route")
async def incident_route(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import RouteDecision

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    decision = await session.scalar(
        select(RouteDecision).where(
            RouteDecision.tenant_id == tenant_id,
            RouteDecision.incident_id == incident_id,
            RouteDecision.decision_no == 1,
        )
    )
    if decision is None:
        return {"route": "UNROUTED", "policy_id": None}
    return {
        "route": decision.route, "policy_id": decision.policy_id,
        "selection_reason": decision.selection_reason,
        "risk_band": decision.risk_band,
        "cost_cap_probes": decision.cost_cap_probes,
        "propensity": decision.propensity,
    }


@app.get("/v1/adapters/simulator/conformance")
async def simulator_conformance_results(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import AdapterConformanceRun

    row = await session.scalar(
        select(AdapterConformanceRun)
        .where(AdapterConformanceRun.adapter_id == "simulator-v1")
        .order_by(AdapterConformanceRun.created_at.desc(), AdapterConformanceRun.id)
        .limit(1)
    )
    if row is None:
        return {"state": "UNTESTED", "production_enablement": False}
    return {
        "run_id": str(row.id), "state": row.state,
        "code_digest": row.code_digest, "suite_version": row.suite_version,
        "results": row.results, "production_enablement": False,
    }


class PolicyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    evaluation_manifest_id: UUID


class RolloutDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: UUID
    cohort_percent: int = Field(ge=1, le=5)


@app.post("/v1/policies", status_code=201)
async def create_policy_draft(
    payload: PolicyDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.policy.registry import PolicyConflict, register_bundle
    from backend.learning.failures import FailureConflict

    try:
        bundle = await register_bundle(
            session, tenant_id, payload.candidate_id,
            payload.evaluation_manifest_id,
        )
    except (PolicyConflict, FailureConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(bundle.id), "content_digest": bundle.content_digest,
            "state": bundle.state, "promotion_eligible": False}


@app.post("/v1/rollouts", status_code=201)
async def create_rollout_draft(
    payload: RolloutDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.policy.registry import PolicyConflict, create_rollout

    try:
        rollout = await create_rollout(
            session, tenant_id, payload.bundle_id, payload.cohort_percent
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(rollout.id), "state": rollout.state,
            "version": rollout.version, "exposure_enabled": False}


@app.post("/v1/rollouts/{rollout_id}/approve")
async def deny_unvalidated_rollout(
    rollout_id: UUID,
    tenant_id: str = Depends(local_tenant),
    operator_identity: str = Depends(local_operator),
) -> dict:
    raise HTTPException(
        409,
        "release gate closed: independent E5 PASS, signer verification, and "
        "release-owner approval are not implemented",
    )


@app.post("/v1/rollouts/{rollout_id}/{action}", status_code=202)
async def stop_policy_rollout(
    rollout_id: UUID,
    action: Literal["pause", "rollback"],
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.policy.registry import PolicyConflict, stop_rollout

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the rollout version")
    try:
        rollout = await stop_rollout(
            session, tenant_id, rollout_id, int(version),
            "PAUSED" if action == "pause" else "ROLLED_BACK",
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if rollout is None:
        raise HTTPException(404, "rollout not found")
    return {"id": str(rollout.id), "state": rollout.state,
            "version": rollout.version, "exposure_enabled": False}


@app.get("/v1/rollouts/{rollout_id}/preview")
async def preview_policy_rollout(
    rollout_id: UUID,
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import PolicyRollout
    from backend.policy.registry import dry_run_assignment

    rollout = await session.get(PolicyRollout, rollout_id)
    if rollout is None or rollout.tenant_id != tenant_id:
        raise HTTPException(404, "rollout not found")
    return {
        "rollout_id": str(rollout.id),
        "would_assign": dry_run_assignment(rollout, tenant_id, incident_id),
        "preview_only": True, "exposure_enabled": False,
    }


@app.get("/v1/incidents")
async def recent_incidents(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[IncidentView]:
    from sqlalchemy import select
    from backend.models import Incident

    rows = (await session.scalars(
        select(Incident)
        .where(Incident.tenant_id == tenant_id)
        .order_by(Incident.created_at.desc(), Incident.id)
        .limit(50)
    )).all()
    return [IncidentView.model_validate(row) for row in rows]


@app.get("/v1/incidents/{incident_id}/events")
async def incident_ui_events(
    incident_id: UUID,
    after_id: int = 0,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import UIEvent

    if after_id < 0:
        raise HTTPException(422, "after_id cannot be negative")
    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    from backend.events import ExpiredCursor, validate_event_cursor

    try:
        await validate_event_cursor(session, tenant_id, incident_id, after_id)
    except ExpiredCursor as exc:
        raise HTTPException(
            410, {"reason": "event history trimmed; refetch snapshot",
                  "min_after_id": exc.min_after_id},
        ) from exc
    rows = (await session.scalars(
        select(UIEvent)
        .where(
            UIEvent.tenant_id == tenant_id,
            UIEvent.incident_id == incident_id,
            UIEvent.id > after_id,
        )
        .order_by(UIEvent.id)
        .limit(100)
    )).all()
    return {
        "events": [
            {"id": event.id, "kind": event.kind,
             "projection_version": event.projection_version,
             "created_at": event.created_at.isoformat()}
            for event in rows
        ],
        "last_id": rows[-1].id if rows else after_id,
    }


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture_id: Literal[
        "recoverable-v1", "false-ack-v1", "already-healthy-v1"
    ]
    seed: int = Field(ge=0, le=1_000_000)


@app.post("/v1/experiments", status_code=201)
async def create_replay_experiment(
    payload: ExperimentRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiments.replay import create_pair

    experiment = await create_pair(session, tenant_id, payload.fixture_id, payload.seed)
    return {"id": str(experiment.id), "status": experiment.status,
            "fixture_digest": experiment.fixture_digest}


@app.get("/v1/experiments/{experiment_id}/results")
async def replay_experiment_results(
    experiment_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiments.replay import pair_report

    result = await pair_report(session, tenant_id, experiment_id)
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


@app.get("/v1/incidents/{incident_id}/advisory")
async def incident_model_advisory(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import ModelAdvisory

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    row = await session.get(ModelAdvisory, incident_id)
    if row is None or row.tenant_id != tenant_id:
        return {"state": "DISABLED_OR_NOT_USED"}
    return {
        "state": row.state,
        "model_id": row.model_id,
        "next_probe": row.next_probe,
        "reason_code": row.reason_code,
        "tokens_reserved": row.tokens_reserved,
        "tokens_used": row.tokens_used,
    }


class FailureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrected_code: Literal["PROBE_DENIED", "EFFECT_FAILED", "EFFECT_UNKNOWN"]
    first_bad_step: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=8, max_length=256)


@app.post("/v1/failures/{case_id}/review", status_code=202)
async def review_failure(
    case_id: UUID,
    payload: FailureReviewRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.learning.failures import FailureConflict, review_classification

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be classification version")
    try:
        case = await review_classification(
            session, tenant_id, case_id, int(version),
            payload.corrected_code, payload.first_bad_step, payload.reason,
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "failure case not found")
    return {
        "id": str(case.id), "cluster_id": str(case.cluster_id),
        "origin_code": case.origin_code,
        "classification_version": case.classification_version,
    }


@app.get("/v1/incidents/{incident_id}/assessments")
async def incident_decision_assessments(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import DecisionAssessment

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = (await session.scalars(
        select(DecisionAssessment)
        .where(
            DecisionAssessment.tenant_id == tenant_id,
            DecisionAssessment.incident_id == incident_id,
        )
        .order_by(DecisionAssessment.step_no)
        .limit(30)
    )).all()
    return [
        {"id": str(row.id), "step_no": row.step_no,
         "feature_version": row.feature_version,
         "threshold_version": row.threshold_version,
         "risk_score": row.risk_score,
         "reason": row.reason, "outcome": row.outcome}
        for row in rows
    ]


@app.get("/v1/incidents/{incident_id}/stream")
async def stream_incident_updates(
    incident_id: UUID,
    request: Request,
    after_id: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    from backend.database import get_session_maker
    from backend.events import (
        STREAM_CAPACITY, ExpiredCursor, stream_incident_events,
        validate_event_cursor,
    )

    if after_id < 0:
        raise HTTPException(400, "after_id cannot be negative")
    if last_event_id is not None:
        if not last_event_id.isdecimal():
            raise HTTPException(400, "Last-Event-ID must be a nonnegative integer")
        after_id = max(after_id, int(last_event_id))
    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    try:
        await validate_event_cursor(session, tenant_id, incident_id, after_id)
    except ExpiredCursor as exc:
        raise HTTPException(
            410, {"reason": "event history trimmed; refetch snapshot",
                  "min_after_id": exc.min_after_id},
        ) from exc

    # The request dependency may outlive StreamingResponse. Release its read
    # transaction before the long-lived stream starts.
    await session.rollback()
    if not STREAM_CAPACITY.claim():
        raise HTTPException(
            503, "incident event stream capacity exhausted",
            headers={"Retry-After": "3"},
        )

    async def bounded_events():
        try:
            async for frame in stream_incident_events(
                get_session_maker(), tenant_id, incident_id, after_id,
                request.is_disconnected,
            ):
                yield frame
        finally:
            STREAM_CAPACITY.release()

    return StreamingResponse(
        bounded_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/incidents/{incident_id}/policy")
async def pinned_incident_policy(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import PolicyAssignment

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    assignment = await session.get(PolicyAssignment, (tenant_id, incident_id))
    if assignment is None:
        return {"state": "UNPINNED", "execution_enabled": False}
    return {
        "state": assignment.source,
        "policy_id": assignment.policy_id,
        "bundle_id": str(assignment.bundle_id) if assignment.bundle_id else None,
        "rollout_id": str(assignment.rollout_id) if assignment.rollout_id else None,
        "execution_enabled": assignment.bundle_id is None and assignment.rollout_id is None,
        "assigned_at": assignment.assigned_at.isoformat(),
    }


@app.get("/v1/operations/model-budget")
async def tenant_model_budget(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.budgets import current_window
    from backend.models import ModelTokenWindow

    limit = get_settings().ai_token_limit_per_minute
    window = await session.get(ModelTokenWindow, (tenant_id, current_window()))
    return {
        "limit_per_minute": limit,
        "reserved_tokens": window.reserved_tokens if window else 0,
        "reported_used_tokens": window.used_tokens if window else 0,
        "provider_enabled": get_settings().ai_advisory_enabled,
    }


class RevokeApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=256)


@app.get("/v1/plans/{plan_id}")
async def plan_preview(
    plan_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import ApprovalGrant, IncidentPlan

    plan = await session.scalar(
        select(IncidentPlan).where(
            IncidentPlan.id == plan_id,
            IncidentPlan.tenant_id == tenant_id,
        )
    )
    if plan is None:
        raise HTTPException(404, "plan not found")
    approval = await session.scalar(
        select(ApprovalGrant).where(
            ApprovalGrant.plan_id == plan_id,
            ApprovalGrant.tenant_id == tenant_id,
        )
    )
    return {
        "id": str(plan.id), "incident_id": str(plan.incident_id),
        "action_id": plan.action_id, "target_id": plan.target_id,
        "plan_digest": plan.plan_digest, "expected_version": plan.expected_version,
        "evidence_ids": plan.evidence_ids, "status": plan.status,
        "version": plan.version,
        "approval": (
            {
                "id": str(approval.id),
                "state": approval.state,
                "version": approval.version,
                "expires_at": approval.expires_at.isoformat(),
                "reviewer": approval.reviewer_id,
                "scope": approval.scope,
                "reason": approval.reason,
            }
            if approval else None
        ),
    }


@app.post("/v1/approvals/{approval_id}/revoke", status_code=202)
async def revoke_plan_approval(
    approval_id: UUID,
    payload: RevokeApprovalRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.plans import PlanConflict, revoke_approval

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be approval version")
    try:
        approval = await revoke_approval(
            session, tenant_id, approval_id, int(version),
            reviewer_identity, payload.reason,
        )
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if approval is None:
        raise HTTPException(404, "approval not found")
    return {
        "id": str(approval.id),
        "state": approval.state,
        "version": approval.version,
    }
