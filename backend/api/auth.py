"""FastAPI routes for server-side principal verification, enterprise identity exchange, and authentication."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal, get_session_factory
from backend.config import get_settings
from backend.domain.auth import (
    ActorKind,
    AuthenticationError,
    Principal,
    PrincipalRole,
    TokenRevocationStore,
    authenticate_user,
    mint_principal_token,
)
from backend.domain.identity_provider import (
    IdentityIntrospectionError,
    MembershipCreateRequest,
    MembershipDeactivateRequest,
    SSRFBlockedError,
    TokenExchangeRequest,
    TokenExchangeResponse,
    TokenIntrospectionClient,
)
from backend.persistence.models import IdentityMembership, Tenant

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    tenant_id: uuid.UUID
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class TokenRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jti: str | None = Field(
        default=None,
        description="Optional specific JTI to revoke. If omitted, current token is revoked.",
    )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    principal: dict[str, Any]


@router.post("/exchange", response_model=TokenExchangeResponse)
async def exchange_token(
    req: TokenExchangeRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> TokenExchangeResponse:
    """Exchanges an upstream RFC 7662 IdP access token for an OpsBlaze tenant-scoped session JWT."""
    settings = getattr(request.app.state, "settings", None) or get_settings()

    # 1. Verify tenant exists
    async with session_factory() as session:
        tenant = await session.get(Tenant, req.tenant_id)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant {req.tenant_id} not found",
            )

    # 2. Introspect upstream token
    idp_client = TokenIntrospectionClient(settings)
    try:
        introspected = await idp_client.introspect_token(req.upstream_token)
    except SSRFBlockedError as ssrf_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Introspection request blocked: {ssrf_err}",
        ) from ssrf_err
    except IdentityIntrospectionError as idp_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Upstream identity verification failed: {idp_err}",
        ) from idp_err

    # 3. Look up authoritative server-managed membership in database
    async with session_factory() as session:
        stmt = select(IdentityMembership).where(
            IdentityMembership.tenant_id == req.tenant_id,
            IdentityMembership.idp_subject == introspected.sub,
        )
        if introspected.iss:
            stmt = stmt.where(IdentityMembership.idp_issuer == introspected.iss)

        membership = (await session.execute(stmt)).scalars().first()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Subject '{introspected.sub}' has no active membership in tenant {req.tenant_id}.",
            )
        if not membership.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Membership for subject '{introspected.sub}' in tenant {req.tenant_id} is deactivated.",
            )

        actor_id = membership.actor_id
        actor_kind = ActorKind(membership.actor_kind)
        roles = frozenset(membership.roles)

    # 4. Determine effective session TTL bounded by upstream expiry
    effective_ttl = req.ttl_seconds
    if introspected.exp is not None:
        remaining_upstream_sec = max(1, introspected.exp - int(time.time()))
        effective_ttl = min(req.ttl_seconds, remaining_upstream_sec)

    # 5. Mint server-authoritative OpsBlaze JWT
    token = mint_principal_token(
        actor_id=actor_id,
        actor_kind=actor_kind,
        tenant_id=req.tenant_id,
        roles=roles,
        secret_key=settings.auth_secret_key or "",
        ttl_seconds=effective_ttl,
        auth_mechanism="IDP_EXCHANGE",
    )

    principal_dict = {
        "actor_id": actor_id,
        "actor_kind": actor_kind.value,
        "tenant_id": str(req.tenant_id),
        "roles": sorted(roles),
        "auth_mechanism": "IDP_EXCHANGE",
    }

    return TokenExchangeResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=effective_ttl,
        principal=principal_dict,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> TokenResponse:
    """Authenticates against server-side identity registry; disabled in production environments."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    if not settings.auth_allow_password_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password-registry login is disabled in production. Use /v1/auth/exchange with enterprise IdP.",
        )

    # Verify tenant exists
    async with session_factory() as session:
        tenant = await session.get(Tenant, req.tenant_id)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant {req.tenant_id} not found",
            )

    try:
        principal = authenticate_user(
            username=req.username,
            password=req.password,
            tenant_id=req.tenant_id,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    token = mint_principal_token(
        actor_id=principal.actor_id,
        actor_kind=principal.actor_kind,
        tenant_id=principal.tenant_id,
        roles=principal.roles,
        secret_key=settings.auth_secret_key or "",
        ttl_seconds=req.ttl_seconds,
        auth_mechanism=principal.auth_mechanism,
    )

    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=req.ttl_seconds,
        principal=principal.to_dict(),
    )


@router.post("/token")
async def legacy_token_endpoint_disabled() -> Any:
    """Public arbitrary token minting is permanently removed to prevent privilege escalation."""
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Public arbitrary token minting is disabled. Use /v1/auth/login or /v1/auth/exchange with verified credentials.",
    )


@router.post("/revoke", status_code=status.HTTP_200_OK)
async def revoke_token(
    req: TokenRevokeRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> dict[str, Any]:
    """Revokes a principal access token immediately."""
    jti_to_revoke = req.jti or principal.jti
    if not jti_to_revoke:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No JTI found in token or request to revoke.",
        )
    TokenRevocationStore.revoke(jti_to_revoke)
    return {"status": "REVOKED", "jti": jti_to_revoke}


@router.get("/me")
async def get_current_principal(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> dict[str, Any]:
    """Returns the verified server-side claims and authorized capabilities of the caller."""
    return principal.to_dict()


@router.post("/memberships", status_code=status.HTTP_201_CREATED)
async def create_membership(
    req: MembershipCreateRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, Any]:
    """Provisions an identity membership for a tenant; requires PLATFORM_ENGINEER role."""
    if PrincipalRole.PLATFORM_ENGINEER.value not in principal.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Membership provisioning requires platform_engineer role.",
        )
    if principal.tenant_id != req.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot provision memberships across different tenants.",
        )

    # Validate roles against PrincipalRole
    valid_roles = {r.value for r in PrincipalRole}
    for role_name in req.roles:
        if role_name not in valid_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid principal role '{role_name}'.",
            )

    membership_id = uuid.uuid4()
    async with session_factory() as session:
        membership = IdentityMembership(
            id=membership_id,
            tenant_id=req.tenant_id,
            idp_issuer=req.idp_issuer,
            idp_subject=req.idp_subject,
            actor_id=req.actor_id,
            actor_kind=req.actor_kind,
            roles=req.roles,
            is_active=True,
        )
        session.add(membership)
        await session.commit()

    return {
        "status": "CREATED",
        "membership_id": str(membership_id),
        "tenant_id": str(req.tenant_id),
        "actor_id": req.actor_id,
        "roles": req.roles,
    }


@router.post("/memberships/{membership_id}/deactivate", status_code=status.HTTP_200_OK)
async def deactivate_membership(
    membership_id: uuid.UUID,
    req: MembershipDeactivateRequest,
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, Any]:
    """Deactivates a tenant membership with 0-second revocation guarantee."""
    if (
        PrincipalRole.PLATFORM_ENGINEER.value not in principal.roles
        and PrincipalRole.INCIDENT_COMMANDER.value not in principal.roles
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Deactivation requires platform_engineer or incident_commander role.",
        )

    async with session_factory() as session:
        membership = await session.get(IdentityMembership, membership_id)
        if membership is None or membership.tenant_id != principal.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Membership {membership_id} not found in tenant.",
            )

        membership.is_active = False
        membership.deactivated_at = datetime.now(UTC)
        membership.deactivated_by = principal.actor_id
        membership.deactivation_reason = req.reason
        await session.commit()

    return {
        "status": "DEACTIVATED",
        "membership_id": str(membership_id),
        "actor_id": membership.actor_id,
        "is_active": False,
        "reason": req.reason,
    }
