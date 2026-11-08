"""FastAPI routes for Feature E3 — Failure Taxonomy, Clusters & Improvement Proposals."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError, validate_reviewer_role
from backend.domain.failure import (
    FailureClusterNotFoundError,
    ProposalConflictError,
    ProposalLifecycleError,
    ProposalNotFoundError,
    ProposalValidationError,
)
from backend.learning.clusters import FailureClusterService
from backend.learning.proposal_service import ProposalService
from backend.persistence.models import FailureCase, FailureCluster

router = APIRouter(tags=["improvements"])


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    return factory  # type: ignore[no-any-return]


from backend.api.auth_deps import get_authenticated_tenant_id

# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------


class FailureCaseResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    outcome_revision_id: uuid.UUID
    cluster_id: uuid.UUID | None
    failure_origin_code: str
    subsystem_version: str
    first_divergent_step_category: str
    first_divergent_step: str | None
    classification_status: str
    classification_version: str
    evidence_digest: str
    created_at: datetime


class FailureClusterResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    signature_hash: str
    failure_origin_code: str
    subsystem_version: str
    first_divergent_step_category: str
    cluster_status: str
    case_count: int
    exemplar_case_id: uuid.UUID | None
    version: int
    quarantine_reason: str | None
    created_at: datetime


class DraftProposalRequest(BaseModel):
    cluster_id: uuid.UUID
    proposal_type: str
    base_policy_version: str = "v1"
    target_component: str
    structured_diff: dict[str, Any]
    hypothesis: str
    expected_benefit: str
    risk_assessment: str
    evaluation_plan: str
    drafter_id: str
    drafter_kind: str = "MODEL"


class SubmitProposalRequest(BaseModel):
    expected_version: int


class ReviewProposalRequest(BaseModel):
    expected_version: int
    reviewer_id: str
    reviewer_role: str
    decision: str
    comments: str | None = None


class QuarantineClusterRequest(BaseModel):
    reason: str


class ImprovementProposalResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    cluster_id: uuid.UUID
    proposal_type: str
    base_policy_version: str
    target_component: str
    structured_diff: dict[str, Any]
    hypothesis: str
    expected_benefit: str
    risk_assessment: str
    evaluation_plan: str
    status: str
    version: int
    drafter_kind: str
    drafter_id: str
    reviewer_id: str | None
    reviewer_role: str | None
    review_decision: str | None
    review_comments: str | None
    rejection_code: str | None
    rejection_reason: str | None
    decided_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/failures", response_model=list[FailureCaseResponse])
async def list_failures(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[datetime | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    origin_code: Annotated[str | None, Query()] = None,
) -> list[FailureCaseResponse]:
    """Retrieve keyset-paginated, tenant-isolated failure cases."""
    async with session_factory() as session:
        query = select(FailureCase).where(FailureCase.tenant_id == tenant_id)
        if cursor:
            query = query.where(FailureCase.created_at < cursor)
        if status:
            query = query.where(FailureCase.classification_status == status)
        if origin_code:
            query = query.where(FailureCase.failure_origin_code == origin_code)

        query = query.order_by(FailureCase.created_at.desc()).limit(limit)
        cases = (await session.execute(query)).scalars().all()

        return [
            FailureCaseResponse(
                id=c.id,
                tenant_id=c.tenant_id,
                incident_id=c.incident_id,
                outcome_revision_id=c.outcome_revision_id,
                cluster_id=c.cluster_id,
                failure_origin_code=c.failure_origin_code,
                subsystem_version=c.subsystem_version,
                first_divergent_step_category=c.first_divergent_step_category,
                first_divergent_step=c.first_divergent_step,
                classification_status=c.classification_status,
                classification_version=c.classification_version,
                evidence_digest=c.evidence_digest,
                created_at=c.created_at,
            )
            for c in cases
        ]


@router.get("/failure-clusters/{cluster_id}", response_model=FailureClusterResponse)
async def get_failure_cluster(
    cluster_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> FailureClusterResponse:
    """Retrieve details, exemplar, and metrics for a tenant failure cluster."""
    async with session_factory() as session:
        cluster = (
            await session.execute(
                select(FailureCluster).where(
                    FailureCluster.id == cluster_id,
                    FailureCluster.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

        if not cluster:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Failure cluster '{cluster_id}' not found",
            )

        return FailureClusterResponse(
            id=cluster.id,
            tenant_id=cluster.tenant_id,
            signature_hash=cluster.signature_hash,
            failure_origin_code=cluster.failure_origin_code,
            subsystem_version=cluster.subsystem_version,
            first_divergent_step_category=cluster.first_divergent_step_category,
            cluster_status=cluster.cluster_status,
            case_count=cluster.case_count,
            exemplar_case_id=cluster.exemplar_case_id,
            version=cluster.version,
            quarantine_reason=cluster.quarantine_reason,
            created_at=cluster.created_at,
        )


@router.post("/failure-clusters/{cluster_id}/quarantine", response_model=FailureClusterResponse)
async def quarantine_failure_cluster(
    cluster_id: uuid.UUID,
    request: QuarantineClusterRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> FailureClusterResponse:
    """Operator endpoint to quarantine a drifting or contested failure cluster."""
    service = FailureClusterService(session_factory)
    try:
        cluster = await service.quarantine_cluster(tenant_id, cluster_id, request.reason)
        return FailureClusterResponse(
            id=cluster.id,
            tenant_id=cluster.tenant_id,
            signature_hash=cluster.signature_hash,
            failure_origin_code=cluster.failure_origin_code,
            subsystem_version=cluster.subsystem_version,
            first_divergent_step_category=cluster.first_divergent_step_category,
            cluster_status=cluster.cluster_status,
            case_count=cluster.case_count,
            exemplar_case_id=cluster.exemplar_case_id,
            version=cluster.version,
            quarantine_reason=cluster.quarantine_reason,
            created_at=cluster.created_at,
        )
    except (FailureClusterNotFoundError, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/improvements/draft", response_model=ImprovementProposalResponse)
async def create_improvement_draft(
    request: DraftProposalRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ImprovementProposalResponse:
    """Draft an improvement proposal with strict server-side allowlist validation."""
    service = ProposalService(session_factory)

    try:
        proposal = await service.create_draft(
            tenant_id=tenant_id,
            cluster_id=request.cluster_id,
            proposal_type=request.proposal_type,
            base_policy_version=request.base_policy_version,
            target_component=request.target_component,
            structured_diff=request.structured_diff,
            hypothesis=request.hypothesis,
            expected_benefit=request.expected_benefit,
            risk_assessment=request.risk_assessment,
            evaluation_plan=request.evaluation_plan,
            drafter_id=request.drafter_id,
            drafter_kind=request.drafter_kind,
        )
    except ProposalValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "PROPOSAL_VALIDATION_FAILED", "rejection_code": getattr(e, "code", None), "detail": str(e)},
        )

    return ImprovementProposalResponse(
        id=proposal.id,
        tenant_id=proposal.tenant_id,
        cluster_id=proposal.cluster_id,
        proposal_type=proposal.proposal_type,
        base_policy_version=proposal.base_policy_version,
        target_component=proposal.target_component,
        structured_diff=proposal.structured_diff,
        hypothesis=proposal.hypothesis,
        expected_benefit=proposal.expected_benefit,
        risk_assessment=proposal.risk_assessment,
        evaluation_plan=proposal.evaluation_plan,
        status=proposal.status,
        version=proposal.version,
        drafter_kind=proposal.drafter_kind,
        drafter_id=proposal.drafter_id,
        reviewer_id=proposal.reviewer_id,
        reviewer_role=proposal.reviewer_role,
        review_decision=proposal.review_decision,
        review_comments=proposal.review_comments,
        rejection_code=proposal.rejection_code,
        rejection_reason=proposal.rejection_reason,
        decided_at=proposal.decided_at,
        created_at=proposal.created_at,
    )


@router.post("/improvements/{proposal_id}/submit", response_model=ImprovementProposalResponse)
async def submit_improvement_proposal(
    proposal_id: uuid.UUID,
    request: SubmitProposalRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ImprovementProposalResponse:
    """Submit a DRAFT proposal for human review under CAS concurrency."""
    service = ProposalService(session_factory)
    try:
        proposal = await service.submit_proposal(
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            expected_version=request.expected_version,
        )
        return ImprovementProposalResponse(
            id=proposal.id,
            tenant_id=proposal.tenant_id,
            cluster_id=proposal.cluster_id,
            proposal_type=proposal.proposal_type,
            base_policy_version=proposal.base_policy_version,
            target_component=proposal.target_component,
            structured_diff=proposal.structured_diff,
            hypothesis=proposal.hypothesis,
            expected_benefit=proposal.expected_benefit,
            risk_assessment=proposal.risk_assessment,
            evaluation_plan=proposal.evaluation_plan,
            status=proposal.status,
            version=proposal.version,
            drafter_kind=proposal.drafter_kind,
            drafter_id=proposal.drafter_id,
            reviewer_id=proposal.reviewer_id,
            reviewer_role=proposal.reviewer_role,
            review_decision=proposal.review_decision,
            review_comments=proposal.review_comments,
            rejection_code=proposal.rejection_code,
            rejection_reason=proposal.rejection_reason,
            decided_at=proposal.decided_at,
            created_at=proposal.created_at,
        )
    except ProposalNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ProposalConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ProposalLifecycleError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if "CAS version conflict" in msg or "version mismatch" in msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


@router.post("/improvements/{proposal_id}/review", response_model=ImprovementProposalResponse)
async def review_improvement_proposal(
    proposal_id: uuid.UUID,
    request: ReviewProposalRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ImprovementProposalResponse:
    """Authorized human review of an improvement proposal with CAS concurrency."""
    try:
        validate_reviewer_role(request.reviewer_role)
    except ApprovalRoleForbiddenError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    service = ProposalService(session_factory)
    try:
        proposal = await service.review_proposal(
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            expected_version=request.expected_version,
            reviewer_id=request.reviewer_id,
            reviewer_role=request.reviewer_role,
            decision=request.decision,
            comments=request.comments,
        )
        return ImprovementProposalResponse(
            id=proposal.id,
            tenant_id=proposal.tenant_id,
            cluster_id=proposal.cluster_id,
            proposal_type=proposal.proposal_type,
            base_policy_version=proposal.base_policy_version,
            target_component=proposal.target_component,
            structured_diff=proposal.structured_diff,
            hypothesis=proposal.hypothesis,
            expected_benefit=proposal.expected_benefit,
            risk_assessment=proposal.risk_assessment,
            evaluation_plan=proposal.evaluation_plan,
            status=proposal.status,
            version=proposal.version,
            drafter_kind=proposal.drafter_kind,
            drafter_id=proposal.drafter_id,
            reviewer_id=proposal.reviewer_id,
            reviewer_role=proposal.reviewer_role,
            review_decision=proposal.review_decision,
            review_comments=proposal.review_comments,
            rejection_code=proposal.rejection_code,
            rejection_reason=proposal.rejection_reason,
            decided_at=proposal.decided_at,
            created_at=proposal.created_at,
        )
    except ProposalNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ProposalConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if "CAS version conflict" in msg or "version mismatch" in msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
