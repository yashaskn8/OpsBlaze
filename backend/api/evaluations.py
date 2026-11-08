"""FastAPI routes for Feature E5 — Candidate-to-Incumbent Regression Gate."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal
from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.auth import AuthorizationForbiddenError, Principal
from backend.domain.evaluation import (
    CandidateBundle,
    CandidateProvenanceType,
    CandidateProvenanceValidationError,
    EvaluationConcurrencyConflictError,
    HoldoutContaminationError,
    IncumbentBundle,
    ManifestLifecycleError,
    ManifestNotFoundError,
    MetricSpec,
    ReportAlreadyReviewedError,
    ReportNotFoundError,
    ReviewDecision,
    SourceEligibilityInvalidatedError,
    TrialStatus,
)
from backend.evaluations.manifest_service import CandidateManifestService
from backend.evaluations.report_service import CandidateReportService
from backend.evaluations.runner import CandidateEvaluationRunner
from backend.persistence.models import (
    CandidateEvaluationManifest,
    CandidateEvaluationReport,
    CandidateEvaluationTrial,
    Tenant,
)

router = APIRouter(tags=["evaluations"])


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    return factory  # type: ignore[no-any-return]


async def get_authenticated_tenant_id(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> uuid.UUID:
    """Enforces tenant isolation by authenticating caller against tenant registry."""
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required X-Tenant-ID authentication header",
        )
    try:
        tenant_uuid = uuid.UUID(x_tenant_id.strip())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed X-Tenant-ID header: must be a valid UUID",
        )

    async with session_factory() as session:
        tenant_exists = await session.get(Tenant, tenant_uuid)
        if not tenant_exists:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authenticated tenant {tenant_uuid} does not exist in authoritative store",
            )

    return tenant_uuid


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------


class CandidateEvaluationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    incumbent_bundle: IncumbentBundle
    candidate_bundle: CandidateBundle
    candidate_provenance_type: CandidateProvenanceType
    source_proposal_id: uuid.UUID | None = None
    source_lesson_ids: list[uuid.UUID] | None = None
    model_id: str = Field(default="gemini-2.0-flash", min_length=1, max_length=64)
    model_version: str = Field(default="v1.0", min_length=1, max_length=64)
    tool_catalog_version: str = Field(default="v1", min_length=1, max_length=64)
    a2_grant_version: str = Field(default="v1", min_length=1, max_length=64)
    fixture_family: str = Field(default="k8s_container_crash", min_length=1, max_length=128)
    fixture_set_digest: str = Field(min_length=1, max_length=64)
    holdout_set_digest: str = Field(min_length=1, max_length=64)
    holdout_incident_ids: list[uuid.UUID] | None = None
    oracle_version: str = Field(default="oracle-v1", min_length=1, max_length=64)
    adjudication_rule_version: str = Field(default="v1", min_length=1, max_length=64)
    source_outcome_revision_ids: list[uuid.UUID] = Field(default_factory=list)
    metric_spec: MetricSpec | None = None
    sample_size_plan: int = Field(default=20, ge=1, le=1000)
    random_seed: int = Field(default=42)
    budget_ceiling_micros: int = Field(default=1000000, ge=1000)
    max_tokens_ceiling: int = Field(default=100000, ge=500)
    created_by: str = Field(default="operator", min_length=1, max_length=128)
    auto_freeze: bool = Field(default=False)


class EvaluationFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(default=1, ge=1)


class EvaluationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(default="eval-worker-api", min_length=1, max_length=128)
    lease_seconds: int = Field(default=60, ge=10, le=600)


class EvaluationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str | None = Field(default=None, max_length=128)
    reviewer_role: str | None = Field(default=None, max_length=64)
    decision: ReviewDecision
    comments: str = Field(default="", max_length=2000)
    expected_version: int = Field(default=1, ge=1)


class CandidateManifestResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    manifest_sha256: str
    incumbent_digest: str
    candidate_digest: str
    candidate_provenance_type: str
    source_proposal_id: uuid.UUID | None
    source_proposal_version: int | None
    source_lesson_ids: list[Any] | None
    model_id: str
    model_version: str
    tool_catalog_version: str
    a2_grant_version: str
    fixture_family: str
    fixture_set_digest: str
    holdout_set_digest: str
    oracle_version: str
    adjudication_rule_version: str
    metric_spec: dict[str, Any]
    primary_endpoint: str
    sample_size_plan: int
    random_seed: int
    budget_ceiling_micros: int
    max_tokens_ceiling: int
    lifecycle_state: str
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    frozen_at: datetime | None


class CandidateTrialResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    manifest_id: uuid.UUID
    pair_id: uuid.UUID
    arm: str
    fixture_id: str
    is_negative_control: bool
    trial_status: str
    outcome_label: str | None
    decision_correct: bool | None
    probes_count: int
    tokens_consumed: int
    cost_micros: int
    duration_ms: float
    validity_state: str
    validity_reason: str | None
    unsafe_action_detected: bool
    unsafe_action_count: int
    worker_id: str | None
    completed_at: datetime | None


class CandidateReportResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    manifest_id: uuid.UUID
    report_status: str
    promotion_eligible: bool
    invalidation_reason: str | None
    total_pairs_planned: int
    total_pairs_completed: int
    invalid_pairs_count: int
    metrics_summary: dict[str, Any]
    mcnemar_statistic: float | None
    mcnemar_p_value: float | None
    recovery_rate_incumbent: float
    recovery_rate_candidate: float
    recovery_delta: float
    recovery_ci_low: float
    recovery_ci_high: float
    cost_per_verified_incumbent: float
    cost_per_verified_candidate: float | None = None
    negative_control_pass_rate: float
    unsafe_action_count: int
    gate_verdicts: list[dict[str, Any]]
    version: int
    reviewer_id: str | None
    reviewer_role: str | None
    review_decision: str | None
    review_comments: str | None
    source_eligibility_rechecked: bool
    reviewed_at: datetime | None
    evaluated_at: datetime


class ManifestDetailResponse(BaseModel):
    manifest: CandidateManifestResponse
    trials_total: int
    trials_completed: int
    latest_report: CandidateReportResponse | None


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.get("/evaluations", response_model=list[CandidateManifestResponse])
async def list_evaluations(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    lifecycle_state: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[CandidateManifestResponse]:
    """Lists candidate evaluation manifests for the authenticated tenant."""
    async with session_factory() as session:
        stmt = (
            select(CandidateEvaluationManifest)
            .where(CandidateEvaluationManifest.tenant_id == tenant_id)
            .order_by(CandidateEvaluationManifest.created_at.desc())
            .limit(limit)
        )
        if lifecycle_state:
            stmt = stmt.where(CandidateEvaluationManifest.lifecycle_state == lifecycle_state)
        rows = (await session.scalars(stmt)).all()
        return [
            CandidateManifestResponse(
                id=m.id,
                tenant_id=m.tenant_id,
                title=m.title,
                manifest_sha256=m.manifest_sha256,
                incumbent_digest=m.incumbent_digest,
                candidate_digest=m.candidate_digest,
                candidate_provenance_type=m.candidate_provenance_type,
                source_proposal_id=m.source_proposal_id,
                source_proposal_version=m.source_proposal_version,
                source_lesson_ids=m.source_lesson_ids,
                model_id=m.model_id,
                model_version=m.model_version,
                tool_catalog_version=m.tool_catalog_version,
                a2_grant_version=m.a2_grant_version,
                fixture_family=m.fixture_family,
                fixture_set_digest=m.fixture_set_digest,
                holdout_set_digest=m.holdout_set_digest,
                oracle_version=m.oracle_version,
                adjudication_rule_version=m.adjudication_rule_version,
                metric_spec=m.metric_spec,
                primary_endpoint=m.primary_endpoint,
                sample_size_plan=m.sample_size_plan,
                random_seed=m.random_seed,
                budget_ceiling_micros=m.budget_ceiling_micros,
                max_tokens_ceiling=m.max_tokens_ceiling,
                lifecycle_state=m.lifecycle_state,
                version=m.version,
                created_by=m.created_by,
                created_at=m.created_at,
                updated_at=m.updated_at,
                frozen_at=m.frozen_at,
            )
            for m in rows
        ]


@router.post("/evaluations", response_model=CandidateManifestResponse, status_code=status.HTTP_201_CREATED)
async def create_evaluation(
    req: CandidateEvaluationCreateRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CandidateManifestResponse:
    """Registers a candidate evaluation manifest with provenance validation and holdout checks."""
    svc = CandidateManifestService(session_factory)
    try:
        manifest = await svc.register_manifest(
            tenant_id=tenant_id,
            title=req.title,
            incumbent_bundle=req.incumbent_bundle,
            candidate_bundle=req.candidate_bundle,
            candidate_provenance_type=req.candidate_provenance_type,
            source_proposal_id=req.source_proposal_id,
            source_lesson_ids=req.source_lesson_ids,
            model_id=req.model_id,
            model_version=req.model_version,
            tool_catalog_version=req.tool_catalog_version,
            a2_grant_version=req.a2_grant_version,
            fixture_family=req.fixture_family,
            fixture_set_digest=req.fixture_set_digest,
            holdout_set_digest=req.holdout_set_digest,
            holdout_incident_ids=req.holdout_incident_ids,
            oracle_version=req.oracle_version,
            adjudication_rule_version=req.adjudication_rule_version,
            source_outcome_revision_ids=req.source_outcome_revision_ids,
            metric_spec=req.metric_spec,
            sample_size_plan=req.sample_size_plan,
            random_seed=req.random_seed,
            budget_ceiling_micros=req.budget_ceiling_micros,
            max_tokens_ceiling=req.max_tokens_ceiling,
            created_by=req.created_by,
            auto_freeze=req.auto_freeze,
        )
    except HoldoutContaminationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"rejection_code": "HOLDOUT_CONTAMINATION", "message": str(e)},
        ) from e
    except CandidateProvenanceValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"rejection_code": "INVALID_PROVENANCE", "message": str(e)},
        ) from e

    return CandidateManifestResponse(
        id=manifest.id,
        tenant_id=manifest.tenant_id,
        title=manifest.title,
        manifest_sha256=manifest.manifest_sha256,
        incumbent_digest=manifest.incumbent_digest,
        candidate_digest=manifest.candidate_digest,
        candidate_provenance_type=manifest.candidate_provenance_type,
        source_proposal_id=manifest.source_proposal_id,
        source_proposal_version=manifest.source_proposal_version,
        source_lesson_ids=manifest.source_lesson_ids,
        model_id=manifest.model_id,
        model_version=manifest.model_version,
        tool_catalog_version=manifest.tool_catalog_version,
        a2_grant_version=manifest.a2_grant_version,
        fixture_family=manifest.fixture_family,
        fixture_set_digest=manifest.fixture_set_digest,
        holdout_set_digest=manifest.holdout_set_digest,
        oracle_version=manifest.oracle_version,
        adjudication_rule_version=manifest.adjudication_rule_version,
        metric_spec=manifest.metric_spec,
        primary_endpoint=manifest.primary_endpoint,
        sample_size_plan=manifest.sample_size_plan,
        random_seed=manifest.random_seed,
        budget_ceiling_micros=manifest.budget_ceiling_micros,
        max_tokens_ceiling=manifest.max_tokens_ceiling,
        lifecycle_state=manifest.lifecycle_state,
        version=manifest.version,
        created_by=manifest.created_by,
        created_at=manifest.created_at,
        updated_at=manifest.updated_at,
        frozen_at=manifest.frozen_at,
    )


@router.get("/evaluations/{id}", response_model=ManifestDetailResponse)
async def get_evaluation(
    id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ManifestDetailResponse:
    """Retrieves manifest details, trial completion counts, and latest report."""
    manifest_svc = CandidateManifestService(session_factory)
    report_svc = CandidateReportService(session_factory)

    try:
        manifest = await manifest_svc.get_manifest(tenant_id=tenant_id, manifest_id=id)
    except ManifestNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    async with session_factory() as session:
        t_total = (
            await session.scalar(
                select(text("COUNT(*)")).select_from(CandidateEvaluationTrial).where(
                    CandidateEvaluationTrial.manifest_id == id
                )
            )
        ) or 0
        t_done = (
            await session.scalar(
                select(text("COUNT(*)")).select_from(CandidateEvaluationTrial).where(
                    CandidateEvaluationTrial.manifest_id == id,
                    CandidateEvaluationTrial.trial_status == TrialStatus.COMPLETED.value,
                )
            )
        ) or 0

    report = await report_svc.get_latest_report(tenant_id=tenant_id, manifest_id=id)
    report_resp = None
    if report:
        report_resp = CandidateReportResponse(
            id=report.id,
            tenant_id=report.tenant_id,
            manifest_id=report.manifest_id,
            report_status=report.report_status,
            promotion_eligible=report.promotion_eligible,
            invalidation_reason=report.invalidation_reason,
            total_pairs_planned=report.total_pairs_planned,
            total_pairs_completed=report.total_pairs_completed,
            invalid_pairs_count=report.invalid_pairs_count,
            metrics_summary=report.metrics_summary,
            mcnemar_statistic=report.mcnemar_statistic,
            mcnemar_p_value=report.mcnemar_p_value,
            recovery_rate_incumbent=report.recovery_rate_incumbent,
            recovery_rate_candidate=report.recovery_rate_candidate,
            recovery_delta=report.recovery_delta,
            recovery_ci_low=report.recovery_ci_low,
            recovery_ci_high=report.recovery_ci_high,
            cost_per_verified_incumbent=report.cost_per_verified_incumbent,
            cost_per_verified_candidate=report.cost_per_verified_candidate,
            negative_control_pass_rate=report.negative_control_pass_rate,
            unsafe_action_count=report.unsafe_action_count,
            gate_verdicts=report.gate_verdicts,
            version=report.version,
            reviewer_id=report.reviewer_id,
            reviewer_role=report.reviewer_role,
            review_decision=report.review_decision,
            review_comments=report.review_comments,
            source_eligibility_rechecked=report.source_eligibility_rechecked,
            reviewed_at=report.reviewed_at,
            evaluated_at=report.evaluated_at,
        )

    return ManifestDetailResponse(
        manifest=CandidateManifestResponse(
            id=manifest.id,
            tenant_id=manifest.tenant_id,
            title=manifest.title,
            manifest_sha256=manifest.manifest_sha256,
            incumbent_digest=manifest.incumbent_digest,
            candidate_digest=manifest.candidate_digest,
            candidate_provenance_type=manifest.candidate_provenance_type,
            source_proposal_id=manifest.source_proposal_id,
            source_proposal_version=manifest.source_proposal_version,
            source_lesson_ids=manifest.source_lesson_ids,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            tool_catalog_version=manifest.tool_catalog_version,
            a2_grant_version=manifest.a2_grant_version,
            fixture_family=manifest.fixture_family,
            fixture_set_digest=manifest.fixture_set_digest,
            holdout_set_digest=manifest.holdout_set_digest,
            oracle_version=manifest.oracle_version,
            adjudication_rule_version=manifest.adjudication_rule_version,
            metric_spec=manifest.metric_spec,
            primary_endpoint=manifest.primary_endpoint,
            sample_size_plan=manifest.sample_size_plan,
            random_seed=manifest.random_seed,
            budget_ceiling_micros=manifest.budget_ceiling_micros,
            max_tokens_ceiling=manifest.max_tokens_ceiling,
            lifecycle_state=manifest.lifecycle_state,
            version=manifest.version,
            created_by=manifest.created_by,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            frozen_at=manifest.frozen_at,
        ),
        trials_total=t_total,
        trials_completed=t_done,
        latest_report=report_resp,
    )


@router.post("/evaluations/{id}/freeze", response_model=CandidateManifestResponse)
async def freeze_evaluation(
    id: uuid.UUID,
    req: EvaluationFreezeRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CandidateManifestResponse:
    """Freezes a manifest into permanently immutable state."""
    svc = CandidateManifestService(session_factory)
    try:
        manifest = await svc.freeze_manifest(tenant_id=tenant_id, manifest_id=id, expected_version=req.expected_version)
    except ManifestNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ManifestLifecycleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except EvaluationConcurrencyConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return CandidateManifestResponse(
        id=manifest.id,
        tenant_id=manifest.tenant_id,
        title=manifest.title,
        manifest_sha256=manifest.manifest_sha256,
        incumbent_digest=manifest.incumbent_digest,
        candidate_digest=manifest.candidate_digest,
        candidate_provenance_type=manifest.candidate_provenance_type,
        source_proposal_id=manifest.source_proposal_id,
        source_proposal_version=manifest.source_proposal_version,
        source_lesson_ids=manifest.source_lesson_ids,
        model_id=manifest.model_id,
        model_version=manifest.model_version,
        tool_catalog_version=manifest.tool_catalog_version,
        a2_grant_version=manifest.a2_grant_version,
        fixture_family=manifest.fixture_family,
        fixture_set_digest=manifest.fixture_set_digest,
        holdout_set_digest=manifest.holdout_set_digest,
        oracle_version=manifest.oracle_version,
        adjudication_rule_version=manifest.adjudication_rule_version,
        metric_spec=manifest.metric_spec,
        primary_endpoint=manifest.primary_endpoint,
        sample_size_plan=manifest.sample_size_plan,
        random_seed=manifest.random_seed,
        budget_ceiling_micros=manifest.budget_ceiling_micros,
        max_tokens_ceiling=manifest.max_tokens_ceiling,
        lifecycle_state=manifest.lifecycle_state,
        version=manifest.version,
        created_by=manifest.created_by,
        created_at=manifest.created_at,
        updated_at=manifest.updated_at,
        frozen_at=manifest.frozen_at,
    )


@router.post("/evaluations/{id}/cancel", response_model=CandidateManifestResponse)
async def cancel_evaluation(
    id: uuid.UUID,
    req: EvaluationCancelRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CandidateManifestResponse:
    """Cancels an active or frozen evaluation run."""
    svc = CandidateManifestService(session_factory)
    try:
        manifest = await svc.cancel_manifest(tenant_id=tenant_id, manifest_id=id, reason=req.reason)
    except ManifestNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ManifestLifecycleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    return CandidateManifestResponse(
        id=manifest.id,
        tenant_id=manifest.tenant_id,
        title=manifest.title,
        manifest_sha256=manifest.manifest_sha256,
        incumbent_digest=manifest.incumbent_digest,
        candidate_digest=manifest.candidate_digest,
        candidate_provenance_type=manifest.candidate_provenance_type,
        source_proposal_id=manifest.source_proposal_id,
        source_proposal_version=manifest.source_proposal_version,
        source_lesson_ids=manifest.source_lesson_ids,
        model_id=manifest.model_id,
        model_version=manifest.model_version,
        tool_catalog_version=manifest.tool_catalog_version,
        a2_grant_version=manifest.a2_grant_version,
        fixture_family=manifest.fixture_family,
        fixture_set_digest=manifest.fixture_set_digest,
        holdout_set_digest=manifest.holdout_set_digest,
        oracle_version=manifest.oracle_version,
        adjudication_rule_version=manifest.adjudication_rule_version,
        metric_spec=manifest.metric_spec,
        primary_endpoint=manifest.primary_endpoint,
        sample_size_plan=manifest.sample_size_plan,
        random_seed=manifest.random_seed,
        budget_ceiling_micros=manifest.budget_ceiling_micros,
        max_tokens_ceiling=manifest.max_tokens_ceiling,
        lifecycle_state=manifest.lifecycle_state,
        version=manifest.version,
        created_by=manifest.created_by,
        created_at=manifest.created_at,
        updated_at=manifest.updated_at,
        frozen_at=manifest.frozen_at,
    )


@router.get("/evaluations/{id}/trials", response_model=list[CandidateTrialResponse])
async def list_trials(
    id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    arm: str | None = None,
    trial_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CandidateTrialResponse]:
    """Retrieves paginated, tenant-isolated trial execution records."""
    async with session_factory() as session:
        stmt = (
            select(CandidateEvaluationTrial)
            .where(
                CandidateEvaluationTrial.tenant_id == tenant_id,
                CandidateEvaluationTrial.manifest_id == id,
            )
            .order_by(CandidateEvaluationTrial.created_at.asc())
            .limit(limit)
        )
        if arm:
            stmt = stmt.where(CandidateEvaluationTrial.arm == arm.upper())
        if trial_status:
            stmt = stmt.where(CandidateEvaluationTrial.trial_status == trial_status.upper())

        rows = (await session.scalars(stmt)).all()
        return [
            CandidateTrialResponse(
                id=t.id,
                tenant_id=t.tenant_id,
                manifest_id=t.manifest_id,
                pair_id=t.pair_id,
                arm=t.arm,
                fixture_id=t.fixture_id,
                is_negative_control=t.is_negative_control,
                trial_status=t.trial_status,
                outcome_label=t.outcome_label,
                decision_correct=t.decision_correct,
                probes_count=t.probes_count,
                tokens_consumed=t.tokens_consumed,
                cost_micros=t.cost_micros,
                duration_ms=t.duration_ms,
                validity_state=t.validity_state,
                validity_reason=t.validity_reason,
                unsafe_action_detected=t.unsafe_action_detected,
                unsafe_action_count=t.unsafe_action_count,
                worker_id=t.worker_id,
                completed_at=t.completed_at,
            )
            for t in rows
        ]


@router.post("/evaluations/{id}/run", response_model=CandidateReportResponse)
async def dispatch_run(
    id: uuid.UUID,
    req: EvaluationRunRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CandidateReportResponse:
    """Dispatches paired evaluation runner over the frozen fixture suite and compiles report."""
    runner = CandidateEvaluationRunner(session_factory)
    report_svc = CandidateReportService(session_factory)

    try:
        await runner.run_evaluation(
            tenant_id=tenant_id,
            manifest_id=id,
            worker_id=req.worker_id,
            lease_seconds=req.lease_seconds,
        )
        report = await report_svc.generate_report(tenant_id=tenant_id, manifest_id=id)
    except ManifestNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ManifestLifecycleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except EvaluationConcurrencyConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return CandidateReportResponse(
        id=report.id,
        tenant_id=report.tenant_id,
        manifest_id=report.manifest_id,
        report_status=report.report_status,
        promotion_eligible=report.promotion_eligible,
        invalidation_reason=report.invalidation_reason,
        total_pairs_planned=report.total_pairs_planned,
        total_pairs_completed=report.total_pairs_completed,
        invalid_pairs_count=report.invalid_pairs_count,
        metrics_summary=report.metrics_summary,
        mcnemar_statistic=report.mcnemar_statistic,
        mcnemar_p_value=report.mcnemar_p_value,
        recovery_rate_incumbent=report.recovery_rate_incumbent,
        recovery_rate_candidate=report.recovery_rate_candidate,
        recovery_delta=report.recovery_delta,
        recovery_ci_low=report.recovery_ci_low,
        recovery_ci_high=report.recovery_ci_high,
        cost_per_verified_incumbent=report.cost_per_verified_incumbent,
        cost_per_verified_candidate=report.cost_per_verified_candidate,
        negative_control_pass_rate=report.negative_control_pass_rate,
        unsafe_action_count=report.unsafe_action_count,
        gate_verdicts=report.gate_verdicts,
        version=report.version,
        reviewer_id=report.reviewer_id,
        reviewer_role=report.reviewer_role,
        review_decision=report.review_decision,
        review_comments=report.review_comments,
        source_eligibility_rechecked=report.source_eligibility_rechecked,
        reviewed_at=report.reviewed_at,
        evaluated_at=report.evaluated_at,
    )


@router.post("/evaluations/{id}/review", response_model=CandidateReportResponse)
async def review_evaluation(
    id: uuid.UUID,
    req: EvaluationReviewRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CandidateReportResponse:
    """Submits human evaluation reviewer decision with CAS optimistic concurrency.

    Reviewer identity and role are derived directly from the authenticated principal.
    Client-asserted reviewer_id or reviewer_role cannot override or expand authority.
    """
    tenant_id = principal.tenant_id
    principal.require_human()
    verified_role = principal.require_evaluation_reviewer()

    if req.reviewer_role:
        norm_req_role = req.reviewer_role.strip().lower()
        if not principal.has_role(norm_req_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Caller lacks asserted role '{req.reviewer_role}'",
            )
        verified_role = norm_req_role

    if req.reviewer_id and req.reviewer_id.strip() != principal.actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot assert reviewer identity '{req.reviewer_id}' (authenticated as '{principal.actor_id}')",
        )

    effective_reviewer_id = principal.actor_id
    effective_reviewer_role = verified_role

    report_svc = CandidateReportService(session_factory)

    # First find the report for this manifest
    async with session_factory() as session:
        report = await session.scalar(
            select(CandidateEvaluationReport).where(
                CandidateEvaluationReport.tenant_id == tenant_id,
                CandidateEvaluationReport.manifest_id == id,
            )
        )
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No evaluation report found for manifest {id}",
            )
        report_id = report.id

    try:
        reviewed_report = await report_svc.submit_review(
            tenant_id=tenant_id,
            report_id=report_id,
            reviewer_id=effective_reviewer_id,
            reviewer_role=effective_reviewer_role,
            decision=req.decision,
            comments=req.comments,
            expected_version=req.expected_version,
        )
    except ApprovalRoleForbiddenError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except AuthorizationForbiddenError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except ReportNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ReportAlreadyReviewedError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except EvaluationConcurrencyConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except SourceEligibilityInvalidatedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return CandidateReportResponse(
        id=reviewed_report.id,
        tenant_id=reviewed_report.tenant_id,
        manifest_id=reviewed_report.manifest_id,
        report_status=reviewed_report.report_status,
        promotion_eligible=reviewed_report.promotion_eligible,
        invalidation_reason=reviewed_report.invalidation_reason,
        total_pairs_planned=reviewed_report.total_pairs_planned,
        total_pairs_completed=reviewed_report.total_pairs_completed,
        invalid_pairs_count=reviewed_report.invalid_pairs_count,
        metrics_summary=reviewed_report.metrics_summary,
        mcnemar_statistic=reviewed_report.mcnemar_statistic,
        mcnemar_p_value=reviewed_report.mcnemar_p_value,
        recovery_rate_incumbent=reviewed_report.recovery_rate_incumbent,
        recovery_rate_candidate=reviewed_report.recovery_rate_candidate,
        recovery_delta=reviewed_report.recovery_delta,
        recovery_ci_low=reviewed_report.recovery_ci_low,
        recovery_ci_high=reviewed_report.recovery_ci_high,
        cost_per_verified_incumbent=reviewed_report.cost_per_verified_incumbent,
        cost_per_verified_candidate=reviewed_report.cost_per_verified_candidate,
        negative_control_pass_rate=reviewed_report.negative_control_pass_rate,
        unsafe_action_count=reviewed_report.unsafe_action_count,
        gate_verdicts=reviewed_report.gate_verdicts,
        version=reviewed_report.version,
        reviewer_id=reviewed_report.reviewer_id,
        reviewer_role=reviewed_report.reviewer_role,
        review_decision=reviewed_report.review_decision,
        review_comments=reviewed_report.review_comments,
        source_eligibility_rechecked=reviewed_report.source_eligibility_rechecked,
        reviewed_at=reviewed_report.reviewed_at,
        evaluated_at=reviewed_report.evaluated_at,
    )
