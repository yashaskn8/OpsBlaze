"""Trusted server-side principal, authentication, and role authorization contracts for OpsBlaze.

Enforces:
- Cryptographic verification of actor claims (no client-asserted identity or roles).
- Strict human-vs-model actor boundary (models/bots can never hold approval or review authority).
- Tenant membership verification and multi-tenant isolation.
- Separation of duties between drafters, evaluation reviewers, and release owners.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from backend.domain.incident import DomainError


class AuthenticationError(DomainError):
    """Raised when authentication credentials are missing, invalid, or expired."""


class AuthorizationForbiddenError(DomainError):
    """Raised when an authenticated principal lacks required authority or violates separation of duties."""


class ActorKind(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    MODEL = "MODEL"
    BOT = "BOT"


class PrincipalRole(StrEnum):
    INCIDENT_COMMANDER = "incident_commander"
    PLATFORM_ENGINEER = "platform_engineer"
    SRE_LEAD = "sre_lead"
    OPERATOR = "operator"
    ONCALL_ENGINEER = "oncall_engineer"
    EVALUATION_OWNER = "evaluation_owner"
    RELEASE_OWNER = "release_owner"


EVALUATION_REVIEWER_ROLES: frozenset[str] = frozenset(
    {
        PrincipalRole.EVALUATION_OWNER.value,
        PrincipalRole.INCIDENT_COMMANDER.value,
        PrincipalRole.PLATFORM_ENGINEER.value,
        PrincipalRole.SRE_LEAD.value,
    }
)

RELEASE_APPROVER_ROLES: frozenset[str] = frozenset(
    {
        PrincipalRole.RELEASE_OWNER.value,
        PrincipalRole.INCIDENT_COMMANDER.value,
    }
)

FORBIDDEN_ACTOR_ROLES: frozenset[str] = frozenset(
    {
        "agent",
        "model",
        "investigator",
        "llm",
        "system",
        "autonomous_agent",
        "bot",
    }
)


@dataclass(frozen=True)
class Principal:
    """Immutable, server-verified principal identity and authority record."""

    actor_id: str
    actor_kind: ActorKind
    tenant_id: uuid.UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    auth_mechanism: str = "bearer_token"

    def is_human(self) -> bool:
        return self.actor_kind == ActorKind.HUMAN

    def has_role(self, role: str) -> bool:
        return role.strip().lower() in self.roles

    def require_human(self) -> None:
        if not self.is_human():
            raise AuthorizationForbiddenError(
                f"Actor '{self.actor_id}' has kind '{self.actor_kind.value}'. "
                "Only verified human operators may hold review or approval authority."
            )

    def require_evaluation_reviewer(self) -> str:
        """Enforces that the principal is human and possesses evaluation review authority."""
        self.require_human()
        matching_roles = [r for r in self.roles if r in EVALUATION_REVIEWER_ROLES]
        if not matching_roles:
            raise AuthorizationForbiddenError(
                f"Principal '{self.actor_id}' lacks required evaluation reviewer role. "
                f"Required one of: {sorted(EVALUATION_REVIEWER_ROLES)}"
            )
        # Return canonical role (prefer evaluation_owner if present)
        if PrincipalRole.EVALUATION_OWNER.value in matching_roles:
            return PrincipalRole.EVALUATION_OWNER.value
        return matching_roles[0]

    def require_release_approver(self, *, drafter_id: str | None = None) -> str:
        """Enforces release approval authority and strict separation of duties."""
        self.require_human()
        matching_roles = [r for r in self.roles if r in RELEASE_APPROVER_ROLES]
        if not matching_roles:
            raise AuthorizationForbiddenError(
                f"Principal '{self.actor_id}' lacks required release approval role. "
                f"Required one of: {sorted(RELEASE_APPROVER_ROLES)}"
            )
        if drafter_id and self.actor_id.strip() == drafter_id.strip():
            raise AuthorizationForbiddenError(
                f"Separation of duties violation: Principal '{self.actor_id}' drafted this release "
                "and cannot self-approve it."
            )
        if PrincipalRole.RELEASE_OWNER.value in matching_roles:
            return PrincipalRole.RELEASE_OWNER.value
        return matching_roles[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind.value,
            "tenant_id": str(self.tenant_id),
            "roles": sorted(self.roles),
            "auth_mechanism": self.auth_mechanism,
            "can_review_evaluation": bool(self.is_human() and any(r in EVALUATION_REVIEWER_ROLES for r in self.roles)),
            "can_approve_release": bool(self.is_human() and any(r in RELEASE_APPROVER_ROLES for r in self.roles)),
        }


# ---------------------------------------------------------------------------
# Cryptographic Token Minting and Verification
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = 4 - (len(s) % 4)
    if padding and padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("ascii"))


def mint_principal_token(
    *,
    actor_id: str,
    actor_kind: ActorKind = ActorKind.HUMAN,
    tenant_id: uuid.UUID,
    roles: list[str] | set[str] | frozenset[str],
    secret_key: str,
    ttl_seconds: int = 3600,
    auth_mechanism: str = "bearer_token",
) -> str:
    """Mints an HMAC-SHA256 signed access token for a verified principal."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    kind_val = actor_kind.value if hasattr(actor_kind, "value") else str(actor_kind)
    payload = {
        "sub": actor_id,
        "kind": kind_val,
        "tenant_id": str(tenant_id),
        "roles": [r.strip().lower() for r in roles],
        "iat": now,
        "exp": now + ttl_seconds,
        "mech": auth_mechanism,
    }

    hdr_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{hdr_b64}.{payload_b64}".encode("ascii")

    sig = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)

    return f"{hdr_b64}.{payload_b64}.{sig_b64}"


def verify_principal_token(token_str: str, secret_key: str) -> Principal:
    """Cryptographically verifies a signed principal token and parses its claims."""
    parts = token_str.strip().split(".")
    if len(parts) != 3:
        raise AuthenticationError("Malformed authentication token: expected 3 dot-separated segments")

    hdr_b64, payload_b64, sig_b64 = parts
    signing_input = f"{hdr_b64}.{payload_b64}".encode("ascii")

    expected_sig = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise AuthenticationError("Invalid token signature encoding") from e

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise AuthenticationError("Cryptographic token signature verification failed: token has been tampered with")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as e:
        raise AuthenticationError("Failed to decode token payload JSON") from e

    # Expiry verification
    now = int(time.time())
    exp = payload.get("exp")
    if exp is None or not isinstance(exp, int) or now > exp:
        raise AuthenticationError("Authentication token has expired")

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise AuthenticationError("Token payload missing valid 'sub' subject claim")

    kind_raw = payload.get("kind", ActorKind.HUMAN.value)
    try:
        actor_kind = ActorKind(kind_raw)
    except ValueError:
        actor_kind = ActorKind.BOT

    tenant_raw = payload.get("tenant_id")
    if not tenant_raw:
        raise AuthenticationError("Token payload missing required 'tenant_id' claim")
    try:
        tenant_uuid = uuid.UUID(str(tenant_raw))
    except (ValueError, TypeError) as e:
        raise AuthenticationError("Token 'tenant_id' claim is not a valid UUID") from e

    roles_raw = payload.get("roles", [])
    if not isinstance(roles_raw, list):
        roles_raw = []
    clean_roles = frozenset(str(r).strip().lower() for r in roles_raw if str(r).strip())

    return Principal(
        actor_id=sub,
        actor_kind=actor_kind,
        tenant_id=tenant_uuid,
        roles=clean_roles,
        auth_mechanism=payload.get("mech", "bearer_token"),
    )
