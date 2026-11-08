"""FastAPI routes for server-side principal verification and authentication."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal, get_session_factory
from backend.config import get_settings
from backend.domain.auth import (
    AuthenticationError,
    Principal,
    TokenRevocationStore,
    authenticate_user,
    mint_principal_token,
)
from backend.persistence.models import Tenant

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    tenant_id: uuid.UUID
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class TokenRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jti: str | None = Field(default=None, description="Optional specific JTI to revoke. If omitted, current token is revoked.")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    principal: dict[str, Any]


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> TokenResponse:
    """Authenticates against server-side identity registry; derives identity and roles strictly on server."""
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

    settings = get_settings()
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
        detail="Public arbitrary token minting is disabled. Use /v1/auth/login with verified server-side credentials.",
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
