"""FastAPI routes for server-side principal verification and authentication."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_principal, get_session_factory
from backend.config import get_settings
from backend.domain.auth import (
    AuthenticationError,
    Principal,
    authenticate_user,
    mint_principal_token,
)
from backend.persistence.models import RevokedToken, Tenant

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
    """Development-only login until a trusted production identity provider is configured."""
    settings = get_settings()
    if settings.opsblaze_env.strip().lower() not in {"development", "test"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Password-registry login is disabled outside development/test. "
                "Configure a trusted identity provider and authoritative tenant-membership store."
            ),
        )

    # The current development registry is not a production identity provider.
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
        detail="Public arbitrary token minting is disabled. Use /v1/auth/login with verified server-side credentials.",
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

    return {"status": "REVOKED", "jti": principal.jti}


@router.get("/me")
async def get_current_principal(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> dict[str, Any]:
    """Returns the verified server-side claims and authorized capabilities of the caller."""
    return principal.to_dict()
