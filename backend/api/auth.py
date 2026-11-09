"""FastAPI routes for server-side principal verification, enterprise identity exchange, and authentication."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    IdentityProviderError,
    MembershipCreateRequest,
    MembershipDeactivateRequest,
    SSRFBlockedError,
    TokenExchangeRequest,
    introspect_access_token,
)
from backend.persistence.models import IdentityMembership, RevokedToken, Tenant

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    tenant_id: uuid.UUID
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class IdentityExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=1, max_length=16384)
    tenant_id: uuid.UUID


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


TokenExchangeResponse = TokenResponse


@router.post("/exchange", response_model=TokenResponse)
async def exchange_token(
    req: TokenExchangeRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> TokenResponse:
    """Exchanges an upstream RFC 7662 IdP access token for an OpsBlaze tenant-scoped session JWT."""
    settings = get_settings()
    app_settings = getattr(request.app.state, "settings", None)
    if app_settings is not None:
        app_has_idp = bool(app_settings.identity_introspection_url or app_settings.idp_introspection_url)
        global_has_idp = bool(settings.identity_introspection_url or settings.idp_introspection_url)
        if app_has_idp and not global_has_idp:
            settings = app_settings

    issuer = (settings.identity_issuer or settings.idp_expected_issuer or "").rstrip("/")
    introspection_url = settings.identity_introspection_url or settings.idp_introspection_url or ""
    client_id = settings.identity_client_id or settings.idp_client_id or ""
    client_secret = settings.identity_client_secret or settings.idp_client_secret or ""
    audience = settings.identity_audience or settings.idp_expected_audience or ""

    required = (
        issuer,
        introspection_url,
        client_id,
        client_secret,
        audience,
    )
    if any(not value for value in required):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trusted identity provider is not configured. Production password login remains disabled.",
        )

    # 1. Verify tenant exists
    async with session_factory() as session:
        tenant = await session.get(Tenant, req.tenant_id)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant {req.tenant_id} not found",
            )

    # 2. Introspect upstream token
    raw_token = req.token_value
    try:
        claims = await introspect_access_token(
            raw_token,
            issuer=issuer,
            introspection_url=introspection_url,
            client_id=client_id,
            client_secret=client_secret,
            audience=audience,
        )
    except SSRFBlockedError as ssrf_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Introspection request blocked: {ssrf_err}",
        ) from ssrf_err
    except (IdentityProviderError, IdentityIntrospectionError) as idp_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Upstream identity verification failed: {idp_err}",
        ) from idp_err

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Upstream identity token contains no subject claim.",
        )

    # 3. Look up authoritative server-managed membership in database
    async with session_factory() as session:
        stmt = select(IdentityMembership).where(
            IdentityMembership.tenant_id == req.tenant_id,
            (IdentityMembership.idp_subject == sub) | (IdentityMembership.subject == sub),
        )
        if claims.get("iss"):
            token_iss = claims["iss"].rstrip("/")
            stmt = stmt.where(
                (IdentityMembership.idp_issuer == token_iss)
                | (IdentityMembership.idp_issuer == claims["iss"])
                | (IdentityMembership.issuer == token_iss)
                | (IdentityMembership.issuer == claims["iss"])
            )

        membership = (await session.execute(stmt)).scalars().first()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No active server-managed membership exists for subject '{sub}' in tenant {req.tenant_id}.",
            )
        if not membership.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Membership for subject '{sub}' in tenant {req.tenant_id} is deactivated.",
            )

        try:
            actor_kind = ActorKind(membership.actor_kind)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Server-managed identity membership has an invalid actor kind.",
            ) from exc

        if actor_kind != ActorKind.HUMAN and membership.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Non-human identity memberships cannot be assigned human authority.",
            )

        actor_id = membership.actor_id
        roles = frozenset(str(r).strip().lower() for r in membership.roles if str(r).strip())

    # 4. Determine effective session TTL bounded by upstream expiry
    effective_ttl = req.ttl_seconds
    exp = claims.get("exp")
    if exp is not None and req.upstream_token is not None:
        remaining_upstream_sec = max(1, exp - int(time.time()))
        effective_ttl = min(req.ttl_seconds, remaining_upstream_sec)

    # 5. Mint server-authoritative OpsBlaze JWT
    token = mint_principal_token(
        actor_id=actor_id,
        actor_kind=actor_kind,
        tenant_id=req.tenant_id,
        roles=roles,
        secret_key=settings.auth_secret_key or "",
        ttl_seconds=effective_ttl,
        auth_mechanism="oauth2_token_introspection",
    )

    principal_dict = {
        "actor_id": actor_id,
        "actor_kind": actor_kind.value,
        "tenant_id": str(req.tenant_id),
        "roles": sorted(roles),
        "auth_mechanism": "oauth2_token_introspection",
    }

    return TokenResponse(
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
    settings = get_settings()
    app_settings = getattr(request.app.state, "settings", None)
    if app_settings is not None and (
        not app_settings.auth_allow_password_login
        or app_settings.opsblaze_env.strip().lower() not in {"development", "test"}
    ):
        settings = app_settings

    env_norm = settings.opsblaze_env.strip().lower()
    if env_norm not in {"development", "test"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Password-registry login is disabled outside development/test. "
                "Configure a trusted identity provider and authoritative tenant-membership store."
            ),
        )
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
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, Any]:
    """Persistently revokes only the caller's current token."""
    if req.jti is not None and req.jti != principal.jti:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A principal may only revoke its own current token.",
        )
    if not principal.jti or principal.expires_at_epoch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The authenticated token is missing its JTI or expiry claim.",
        )

    expires_at = datetime.fromtimestamp(principal.expires_at_epoch, tz=UTC)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The authenticated token has already expired.",
        )

    async with session_factory() as session:
        statement = pg_insert(RevokedToken).values(
            jti=principal.jti,
            actor_id=principal.actor_id,
            tenant_id=principal.tenant_id,
            expires_at=expires_at,
        ).on_conflict_do_update(
            index_elements=[RevokedToken.jti],
            set_={
                "actor_id": principal.actor_id,
                "tenant_id": principal.tenant_id,
                "expires_at": expires_at,
            },
        )
        await session.execute(statement)
        await session.commit()

    TokenRevocationStore.revoke(principal.jti)

    return {"status": "REVOKED", "jti": principal.jti}


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
