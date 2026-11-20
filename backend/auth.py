"""Authenticated tenant and role boundary for the OpsBlaze API.

Development uses unshared local bootstrap secrets. Production accepts only
signed RS256 bearer access tokens from an explicitly pinned HTTPS issuer and
JWKS URL. External keys are fetched by PyJWT outside database transactions.
"""

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import RevokedIdentity
from jwt import PyJWKClient

from backend.settings import Settings, get_settings

TENANT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
ALLOWED_ROLES = frozenset({"viewer", "operator", "reviewer", "security_admin"})


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str  # Stable non-reversible audit pseudonym, never a raw email.
    roles: frozenset[str]


@lru_cache(maxsize=8)
def jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, timeout=5, cache_jwk_set=True, lifespan=300)


def configured_identity(settings: Settings) -> bool:
    values = (settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    if any(not value for value in values):
        return False
    issuer = urlparse(settings.oidc_issuer)
    jwks = urlparse(settings.oidc_jwks_url)
    return (
        issuer.scheme == jwks.scheme == "https"
        and bool(issuer.hostname and jwks.hostname)
        and issuer.hostname == jwks.hostname
        and not issuer.username and not jwks.username
        and not issuer.password and not jwks.password
        and not jwks.fragment
    )


def signing_key(token: str, settings: Settings):
    # A pinned JWKS endpoint is the only network input; JWT headers never
    # provide URLs, allowed algorithms, issuer, or key retrieval locations.
    return jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token).key


def verify_bearer(token: str, settings: Settings) -> Principal:
    if len(token) > 8192 or token.count(".") != 2:
        raise ValueError("invalid token shape")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("unsupported JWT signing algorithm")
    claims = jwt.decode(
        token, signing_key(token, settings),
        algorithms=["RS256"],
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        options={"require": ["iss", "sub", "aud", "iat", "exp"]},
        leeway=30,
    )
    tenant_id = claims.get("tenant_id")
    subject = claims.get("sub")
    roles = claims.get("roles")
    issued = claims.get("iat")
    expires = claims.get("exp")
    if (
        claims.get("aud") != settings.oidc_audience
        or type(issued) is not int
        or type(expires) is not int
        or not 0 < expires - issued <= 3600
        or not isinstance(tenant_id, str)
        or TENANT_PATTERN.fullmatch(tenant_id) is None
        or not isinstance(subject, str) or not 1 <= len(subject) <= 256
        or not isinstance(roles, list)
        or not roles or any(not isinstance(role, str) for role in roles)
        or not set(roles).issubset(ALLOWED_ROLES)
    ):
        raise ValueError("token identity or roles are invalid")
    pseudonym = hashlib.sha256(
        f"{settings.oidc_issuer}|{subject}".encode()
    ).hexdigest()[:32]
    return Principal(tenant_id, f"oidc:{pseudonym}", frozenset(roles))


async def get_principal(
    x_tenant_id: str | None = Header(default=None),
    x_opsblaze_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if settings.environment == "production":
        if not configured_identity(settings):
            raise HTTPException(503, "production identity provider is not configured")
        if x_opsblaze_token is not None:
            raise HTTPException(401, "development credentials cannot authenticate production")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "verified bearer access token required")
        token = authorization[len("Bearer "):].strip()
        if not token or " " in token:
            raise HTTPException(401, "invalid bearer token")
        try:
            # PyJWKClient uses blocking HTTPS: never block the FastAPI loop.
            actor = await asyncio.to_thread(verify_bearer, token, settings)
        except (jwt.PyJWTError, ValueError, OSError, TimeoutError) as exc:
            raise HTTPException(401, "invalid or expired bearer token") from exc
        if x_tenant_id is not None and x_tenant_id != actor.tenant_id:
            raise HTTPException(403, "requested tenant does not match token")
        # A signed JWT can remain valid after role removal at the IdP.
        # Recheck the authoritative DB deny-list on every production call,
        # and close the read transaction before endpoint mutations begin.
        try:
            revoked = await session.get(
                RevokedIdentity, (actor.tenant_id, actor.subject)
            )
        except Exception as exc:
            raise HTTPException(503, "identity revocation store unavailable") from exc
        finally:
            await session.rollback()
        if revoked is not None:
            raise HTTPException(403, "identity access revoked")
        return actor

    if not settings.local_token:
        raise HTTPException(503, "local API token not configured")
    if not x_tenant_id or not x_opsblaze_token:
        raise HTTPException(422, "X-Tenant-Id and X-OpsBlaze-Token required")
    if TENANT_PATTERN.fullmatch(x_tenant_id) is None:
        raise HTTPException(422, "invalid tenant identifier")
    if not secrets.compare_digest(x_opsblaze_token, settings.local_token):
        raise HTTPException(401, "invalid local API token")
    return Principal(
        tenant_id=x_tenant_id,
        subject="DEV_OPERATOR_CREDENTIAL",
        roles=frozenset({"operator", "viewer"}),
    )


def local_tenant(actor: Principal = Depends(get_principal)) -> str:
    if not actor.roles.intersection(ALLOWED_ROLES):
        raise HTTPException(403, "tenant access not granted")
    return actor.tenant_id


def local_operator(actor: Principal = Depends(get_principal)) -> str:
    if "operator" not in actor.roles:
        raise HTTPException(403, "operator role required")
    return actor.subject


def local_reviewer(
    actor: Principal = Depends(get_principal),
    reviewer_token: str | None = Header(
        default=None, alias="X-OpsBlaze-Reviewer-Token"
    ),
    settings: Settings = Depends(get_settings),
) -> str:
    if settings.environment == "production":
        if (
            "reviewer" not in actor.roles
            or actor.roles.intersection({"operator", "security_admin"})
        ):
            raise HTTPException(403, "independent reviewer role required")
        return actor.subject
    if (
        not settings.review_token or not settings.local_token
        or secrets.compare_digest(settings.review_token, settings.local_token)
    ):
        raise HTTPException(503, "distinct local reviewer credential is not configured")
    if reviewer_token is None or not secrets.compare_digest(
        reviewer_token, settings.review_token
    ):
        raise HTTPException(403, "reviewer credential required")
    return "DEV_REVIEWER_CREDENTIAL"


def local_security_admin(
    actor: Principal = Depends(get_principal),
    settings: Settings = Depends(get_settings),
) -> str:
    # A separate authority cannot also hold operator/reviewer capabilities.
    if (
        settings.environment != "production"
        or "security_admin" not in actor.roles
        or actor.roles.intersection({"operator", "reviewer"})
    ):
        raise HTTPException(403, "independent security administrator required")
    return actor.subject
