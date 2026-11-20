"""Strict, separately-audienced signed IdP revocation event verification.

This is an integration contract, not a claim that any particular IdP is connected.
The webhook never trusts unsigned JSON or the caller's tenant header. Only
IDENTITY_ACCESS_REVOKED is supported; partial role changes conservatively
revoke the whole API identity until a trusted administrative reset procedure.
"""

import hashlib
import re
import time
from dataclasses import dataclass

import jwt

from backend import auth
from backend.settings import Settings

EVENT_TYPE = "IDENTITY_ACCESS_REVOKED"
JTI_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\\Z")


@dataclass(frozen=True)
class VerifiedIdentityEvent:
    tenant_id: str
    actor_id: str
    event_digest: str


def verify_identity_event(token: str, settings: Settings) -> VerifiedIdentityEvent:
    if (
        settings.environment != "production"
        or not auth.configured_identity(settings)
        or not settings.idp_event_audience
        or settings.idp_event_audience == settings.oidc_audience
    ):
        raise ValueError("separate trusted IdP event audience is not configured")
    if len(token) > 8192 or token.count(".") != 2:
        raise ValueError("invalid security event token shape")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("unsupported event signature")
    claims = jwt.decode(
        token, auth.signing_key(token, settings),
        algorithms=["RS256"],
        issuer=settings.oidc_issuer,
        audience=settings.idp_event_audience,
        options={
            "require": [
                "iss", "sub", "aud", "iat", "exp", "jti",
                "tenant_id", "event_type",
            ],
        },
        leeway=30,
    )
    now = int(time.time())
    issued = claims["iat"]
    expiry = claims["exp"]
    subject = claims["sub"]
    tenant = claims["tenant_id"]
    jti = claims["jti"]
    if (
        claims.get("aud") != settings.idp_event_audience
        or claims.get("event_type") != EVENT_TYPE
        or type(issued) is not int
        or type(expiry) is not int
        or not now - 300 <= issued <= now + 30
        or not issued < expiry <= issued + 300
        or not isinstance(tenant, str)
        or auth.TENANT_PATTERN.fullmatch(tenant) is None
        or not isinstance(subject, str)
        or not 1 <= len(subject) <= 256
        or not isinstance(jti, str)
        or JTI_PATTERN.fullmatch(jti) is None
    ):
        raise ValueError("invalid identity revocation event claims")
    actor_id = "oidc:" + hashlib.sha256(
        f"{settings.oidc_issuer}|{subject}".encode()
    ).hexdigest()[:32]
    event_digest = hashlib.sha256(
        f"{settings.oidc_issuer}|{jti}".encode()
    ).hexdigest()
    return VerifiedIdentityEvent(tenant, actor_id, event_digest)
