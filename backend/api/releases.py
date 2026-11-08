"""FastAPI routes for Feature E6 — Versioned Policy Canary, Release Control & Rollback."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal
from backend.domain.auth import AuthorizationForbiddenError, Principal
from backend.domain.release import (
    ActiveReleaseConflictError,
    BundleDigestMismatchError,
    BundleNotFoundError,
    BundleNotSealedError,
    BundleSignatureInvalidError,
    CanaryMetricsResponse,
    E5EligibilityInvalidatedError,
    InvalidReleaseTransitionError,
    KillSwitchRequest,
    PolicyBundleCreateRequest,
    PolicyBundleResponse,
    PolicyReleaseCreateRequest,
    PolicyReleaseResponse,
    ReleaseAuditEventResponse,
    ReleaseNotFoundError,
    ReleaseReviewRequest,
    ReleaseSeparationOfDutiesError,
    ReleaseTransitionRequest,
    RollbackRequest,
    RolloutPercentageOutOfBoundsError,
    StaleVersionConflictError,
)
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.guardrails import ReleaseGuardrailService
from backend.releases.release_service import PolicyReleaseService

router = APIRouter(tags=["releases"])


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory not initialized.",
        )
    return cast(async_sessionmaker[AsyncSession], factory)


def get_primary_role(principal: Principal) -> str:
    return sorted(principal.roles)[0] if principal.roles else "operator"


# ==============================================================================
# Policy Bundle Endpoints
# ==============================================================================

@router.post(
    "/v1/bundles",
    response_model=PolicyBundleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_bundle(
    body: PolicyBundleCreateRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Create a new draft policy bundle."""
    session_factory = get_session_factory(request)
    bundle_svc = PolicyBundleService(session_factory)
    try:
        bundle = await bundle_svc.create_bundle(
            tenant_id=principal.tenant_id,
            bundle_name=body.bundle_name,
            base_policy_version=body.base_policy_version,
            target_component=body.target_component,
            created_by=principal.actor_id,
            model_id=body.model_id,
            model_version=body.model_version,
            tool_catalog_version=body.tool_catalog_version,
            a2_grant_version=body.a2_grant_version,
            source_provenance_digest=body.source_provenance_digest,
            proposal_diff=body.proposal_diff,
            prompt_templates=body.prompt_templates,
            active_lesson_ids=body.active_lesson_ids,
            source_manifest_id=body.source_manifest_id,
            source_report_id=body.source_report_id,
        )
        return bundle
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/bundles/{bundle_id}/seal",
    response_model=PolicyBundleResponse,
)
async def seal_bundle(
    bundle_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Seal and cryptographically sign a policy bundle."""
    session_factory = get_session_factory(request)
    bundle_svc = PolicyBundleService(session_factory)
    try:
        return await bundle_svc.seal_bundle(
            tenant_id=principal.tenant_id,
            bundle_id=bundle_id,
        )
    except BundleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (BundleDigestMismatchError, BundleSignatureInvalidError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get(
    "/v1/bundles/{bundle_id}",
    response_model=PolicyBundleResponse,
)
async def get_bundle(
    bundle_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Retrieve policy bundle details."""
    session_factory = get_session_factory(request)
    bundle_svc = PolicyBundleService(session_factory)
    try:
        return await bundle_svc.get_bundle(
            tenant_id=principal.tenant_id,
            bundle_id=bundle_id,
        )
    except BundleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/v1/bundles",
    response_model=list[PolicyBundleResponse],
)
async def list_bundles(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
    target_component: str | None = None,
    bundle_status: str | None = Query(None, alias="status"),
    limit: int = 50,
) -> Any:
    """List policy bundles for the tenant."""
    session_factory = get_session_factory(request)
    bundle_svc = PolicyBundleService(session_factory)
    return await bundle_svc.list_bundles(
        tenant_id=principal.tenant_id,
        target_component=target_component,
        status=bundle_status,
        limit=limit,
    )


@router.post(
    "/v1/bundles/bootstrap",
    response_model=PolicyBundleResponse,
)
async def bootstrap_incumbent_bundle(
    target_component: str,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Ensure a sealed baseline incumbent bundle exists for component."""
    session_factory = get_session_factory(request)
    bundle_svc = PolicyBundleService(session_factory)
    return await bundle_svc.ensure_bootstrap_incumbent(
        tenant_id=principal.tenant_id,
        target_component=target_component,
        created_by=principal.actor_id,
    )


# ==============================================================================
# Policy Release Endpoints
# ==============================================================================

@router.post(
    "/v1/releases",
    response_model=PolicyReleaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_release(
    body: PolicyReleaseCreateRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Create a new policy release in DRAFT state."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.create_release(
            tenant_id=principal.tenant_id,
            target_component=body.target_component,
            bundle_id=body.bundle_id,
            incumbent_bundle_id=body.incumbent_bundle_id,
            drafter_id=principal.actor_id,
            rollout_salt=body.rollout_salt,
        )
    except BundleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except BundleNotSealedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/submit",
    response_model=PolicyReleaseResponse,
)
async def submit_release_for_approval(
    release_id: uuid.UUID,
    body: ReleaseTransitionRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Submit a draft release for human approval."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.submit_for_approval(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/review",
    response_model=PolicyReleaseResponse,
)
async def review_release(
    release_id: uuid.UUID,
    body: ReleaseReviewRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Review decision (APPROVED or REJECTED) on a release. Enforces separation of duties."""
    try:
        reviewer_role = principal.require_release_approver()
    except AuthorizationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.review_release(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            reviewer_id=principal.actor_id,
            reviewer_role=reviewer_role,
            decision=body.decision,
            decision_comments=body.decision_comments,
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ReleaseSeparationOfDutiesError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except E5EligibilityInvalidatedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/activate-shadow",
    response_model=PolicyReleaseResponse,
)
async def activate_shadow(
    release_id: uuid.UUID,
    body: ReleaseTransitionRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Activate shadow mode for an approved release."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.activate_shadow(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ActiveReleaseConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/activate-canary",
    response_model=PolicyReleaseResponse,
)
async def activate_canary(
    release_id: uuid.UUID,
    body: ReleaseTransitionRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Activate canary mode with percentage allocation."""
    pct = body.canary_percentage if body.canary_percentage is not None else 10
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.activate_canary(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            canary_percentage=pct,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ActiveReleaseConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except RolloutPercentageOutOfBoundsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/update-canary",
    response_model=PolicyReleaseResponse,
)
async def update_canary_percentage(
    release_id: uuid.UUID,
    body: ReleaseTransitionRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Update canary percentage on an active canary release."""
    if body.canary_percentage is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="canary_percentage is required.")
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.update_canary_percentage(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            new_percentage=body.canary_percentage,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except RolloutPercentageOutOfBoundsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/promote-active",
    response_model=PolicyReleaseResponse,
)
async def promote_active(
    release_id: uuid.UUID,
    body: ReleaseTransitionRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Promote a successful canary release to 100% ACTIVE production traffic."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.promote_active(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/rollback",
    response_model=PolicyReleaseResponse,
)
async def trigger_rollback(
    release_id: uuid.UUID,
    body: RollbackRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Trigger rollback from any active state to ROLLED_BACK."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.trigger_rollback(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            rollback_reason=body.rollback_reason,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidReleaseTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/v1/releases/{release_id}/kill-switch",
    response_model=PolicyReleaseResponse,
)
async def trip_kill_switch(
    release_id: uuid.UUID,
    body: KillSwitchRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Emergency Kill Switch: instantly terminates candidate traffic and rolls back."""
    try:
        principal.require_human()
    except AuthorizationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    allowed_roles = {"operator", "sre_lead", "release_owner", "incident_commander", "platform_engineer"}
    if not (principal.roles & allowed_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Principal roles {sorted(principal.roles)} not authorized to trip the emergency kill switch.",
        )

    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.trip_kill_switch(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            expected_version=body.expected_version,
            justification=body.justification,
            actor_id=principal.actor_id,
            actor_role=get_primary_role(principal),
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except StaleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get(
    "/v1/releases/{release_id}",
    response_model=PolicyReleaseResponse,
)
async def get_release(
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Get release details."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    try:
        return await release_svc.get_release(
            tenant_id=principal.tenant_id,
            release_id=release_id,
        )
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/v1/releases",
    response_model=list[PolicyReleaseResponse],
)
async def list_releases(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
    target_component: str | None = None,
    release_state: str | None = Query(None, alias="state"),
    limit: int = 50,
) -> Any:
    """List policy releases for the tenant."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    return await release_svc.list_releases(
        tenant_id=principal.tenant_id,
        target_component=target_component,
        state=release_state,
        limit=limit,
    )


@router.get(
    "/v1/releases/{release_id}/audits",
    response_model=list[ReleaseAuditEventResponse],
)
async def list_release_audits(
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
    limit: int = 100,
) -> Any:
    """List audit events for a release."""
    session_factory = get_session_factory(request)
    release_svc = PolicyReleaseService(session_factory)
    return await release_svc.list_release_audits(
        tenant_id=principal.tenant_id,
        release_id=release_id,
        limit=limit,
    )


@router.get(
    "/v1/releases/{release_id}/metrics",
    response_model=CanaryMetricsResponse,
)
async def get_canary_metrics(
    release_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Compute and return live canary metrics."""
    session_factory = get_session_factory(request)
    guardrail_svc = ReleaseGuardrailService(session_factory)
    try:
        return await guardrail_svc.compute_canary_metrics(
            tenant_id=principal.tenant_id,
            release_id=release_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


class GuardrailCheckRequest(BaseModel):
    simulated_invariant_breaches: int = 0
    invariant_breach_detail: str = ""


@router.post(
    "/v1/releases/{release_id}/guardrail-check",
)
async def run_guardrail_check(
    release_id: uuid.UUID,
    body: GuardrailCheckRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    request: Request,
) -> Any:
    """Run guardrail check and execute automated rollback if safety rules are violated."""
    session_factory = get_session_factory(request)
    guardrail_svc = ReleaseGuardrailService(session_factory)
    try:
        rolled_back, reason = await guardrail_svc.evaluate_and_enforce_guardrails(
            tenant_id=principal.tenant_id,
            release_id=release_id,
            simulated_invariant_breaches=body.simulated_invariant_breaches,
            invariant_breach_detail=body.invariant_breach_detail,
        )
        return {
            "release_id": str(release_id),
            "rolled_back": rolled_back,
            "reason": reason,
        }
    except ReleaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
