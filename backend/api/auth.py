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
    ActorKind,
    FORBIDDEN_ACTOR_ROLES,
    Principal,
    mint_principal_token,
)
from backend.persistence.models import Tenant

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class TokenMintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: ActorKind = Field(default=ActorKind.HUMAN)
    tenant_id: uuid.UUID
    roles: list[str] = Field(min_length=1)
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    principal: dict[str, Any]


@router.post("/token", response_model=TokenResponse)
async def mint_token(
    req: TokenMintRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> TokenResponse:
    """Mints an authenticated, cryptographically signed Bearer token for a verified principal."""
    # Verify tenant exists
    async with session_factory() as session:
        tenant = await session.get(Tenant, req.tenant_id)
        if not tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant {req.tenant_id} not found",
            )

    # Validate roles: cannot be forbidden model/agent roles
    clean_roles = []
    for r in req.roles:
        norm = r.strip().lower()
        if norm in FORBIDDEN_ACTOR_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Role '{r}' is a non-human/model role and cannot be granted human authority",
            )
        clean_roles.append(norm)

    settings = get_settings()
    token = mint_principal_token(
        actor_id=req.actor_id,
        actor_kind=req.actor_kind,
        tenant_id=req.tenant_id,
        roles=clean_roles,
        secret_key=settings.auth_secret_key,
        ttl_seconds=req.ttl_seconds,
    )

    principal = Principal(
        actor_id=req.actor_id,
        actor_kind=req.actor_kind,
        tenant_id=req.tenant_id,
        roles=frozenset(clean_roles),
    )

    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=req.ttl_seconds,
        principal=principal.to_dict(),
    )


@router.get("/me")
async def get_current_principal(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> dict[str, Any]:
    """Returns the verified server-side claims and authorized capabilities of the caller."""
    return principal.to_dict()
