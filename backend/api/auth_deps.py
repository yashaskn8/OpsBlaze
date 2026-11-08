"""FastAPI authentication dependencies enforcing trusted server-side principal verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.domain.auth import (
    AuthenticationError,
    Principal,
    verify_principal_token,
)
from backend.persistence.models import RevokedToken, Tenant


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    return factory  # type: ignore[no-any-return]


async def get_authenticated_principal(
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> Principal:
    """Derives and cryptographically verifies the calling principal.

    Fails closed if the authorization token is missing, expired, revoked, or tampered with.
    Cross-checks tenant membership and guarantees tenant isolation.
    """
    settings = get_settings()

    if not authorization or not authorization.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required Authorization header with Bearer token",
        )

    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization scheme: expected 'Bearer <signed_token>'",
        )

    token_str = parts[1]
    try:
        principal = verify_principal_token(token_str, settings.auth_secret_key or "")
    except AuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {e}",
        ) from e

    # Verify durable revocation and tenant existence in one authoritative DB session.
    # If the identity authority cannot be reached, fail closed with 503 rather than
    # silently downgrading to a misleading 401 or continuing without revocation checks.
    try:
        async with session_factory() as session:
            if principal.jti:
                revoked_jti = (
                    await session.execute(
                        select(RevokedToken.jti).where(
                            RevokedToken.jti == principal.jti,
                            RevokedToken.expires_at > datetime.now(UTC),
                        )
                    )
                ).scalar_one_or_none()
                if revoked_jti is not None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication token has been revoked.",
                    )

            tenant = await session.get(Tenant, principal.tenant_id)
            if not tenant:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authenticated tenant does not exist in authoritative store.",
                )
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication authority is temporarily unavailable.",
        ) from exc

    # If X-Tenant-ID header is also provided, ensure strict match (prevent cross-tenant operations)
    if x_tenant_id and x_tenant_id.strip():
        try:
            hdr_tenant = uuid.UUID(x_tenant_id.strip())
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed X-Tenant-ID header",
            )
        if hdr_tenant != principal.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cross-tenant access forbidden: token tenant {principal.tenant_id} != header {hdr_tenant}",
            )

    return principal


async def get_authenticated_tenant_id(
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> uuid.UUID:
    """Derives verified tenant ID strictly from the cryptographically verified calling principal."""
    return principal.tenant_id


async def get_optional_principal(
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> Principal | None:
    """Returns verified principal if Authorization header is present, else None."""
    if not authorization or not authorization.strip():
        return None
    try:
        return await get_authenticated_principal(
            request=request,
            session_factory=session_factory,
            authorization=authorization,
            x_tenant_id=x_tenant_id,
        )
    except HTTPException:
        return None
