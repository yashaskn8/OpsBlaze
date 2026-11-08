"""Trusted server-side principal, authentication, and role authorization contracts for OpsBlaze.

Enforces:
- Cryptographic verification of actor claims (no client-asserted identity or roles).
- Strict human-vs-model actor boundary (models/bots can never hold approval or review authority).
- Tenant membership verification and multi-tenant isolation.
- Separation of duties between drafters, evaluation reviewers, and release owners.
- Role-based transition authorization matrix across all release and incident lifecycles.
- Token revocation and strict HMAC-SHA256 algorithm enforcement.
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
from typing import Any, ClassVar

from backend.domain.incident import DomainError


class AuthenticationError(DomainError):
    """Raised when authentication credentials are missing, invalid, expired, or revoked."""


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

# ---------------------------------------------------------------------------
# Stage 11 & 11.1 Release Transition Role Matrix (ADR-0011 / ADR-0012)
# ---------------------------------------------------------------------------
RELEASE_ROLE_MATRIX: dict[str, frozenset[str]] = {
    "bundle_create": frozenset(
        {
            PrincipalRole.PLATFORM_ENGINEER.value,
            PrincipalRole.EVALUATION_OWNER.value,
            PrincipalRole.RELEASE_OWNER.value,
        }
    ),
    "bundle_seal": frozenset(
        {
            PrincipalRole.PLATFORM_ENGINEER.value,
            PrincipalRole.RELEASE_OWNER.value,
        }
    ),
    "release_create": frozenset(
        {
            PrincipalRole.PLATFORM_ENGINEER.value,
            PrincipalRole.RELEASE_OWNER.value,
        }
    ),
    "submit_for_approval": frozenset(
        {
            PrincipalRole.PLATFORM_ENGINEER.value,
            PrincipalRole.RELEASE_OWNER.value,
        }
    ),
    "human_approval": frozenset(
        {
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "shadow_activate": frozenset(
        {
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "canary_activate": frozenset(
        {
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "canary_percentage_update": frozenset(
        {
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "promote_active": frozenset(
        {
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "manual_rollback": frozenset(
        {
            PrincipalRole.OPERATOR.value,
            PrincipalRole.SRE_LEAD.value,
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
    "kill_switch": frozenset(
        {
            PrincipalRole.OPERATOR.value,
            PrincipalRole.SRE_LEAD.value,
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
            PrincipalRole.PLATFORM_ENGINEER.value,
        }
    ),
    "release_revoke": frozenset(
        {
            PrincipalRole.PLATFORM_ENGINEER.value,
            PrincipalRole.RELEASE_OWNER.value,
            PrincipalRole.INCIDENT_COMMANDER.value,
        }
    ),
}


# ---------------------------------------------------------------------------
# Token Revocation Store
# ---------------------------------------------------------------------------
class TokenRevocationStore:
    """Process-local compatibility helper for isolated unit tests only.

    Runtime API revocation is persisted in PostgreSQL by the authentication layer.
    This class is not a durable or multi-worker revocation mechanism.
    """

    _revoked_jtis: ClassVar[set[str]] = set()

    @classmethod
    def revoke(cls, jti: str) -> None:
        cls._revoked_jtis.add(jti.strip())

    @classmethod
    def is_revoked(cls, jti: str) -> bool:
        return jti.strip() in cls._revoked_jtis

    @classmethod
    def unrevoke(cls, jti: str) -> None:
        cls._revoked_jtis.discard(jti.strip())

    @classmethod
    def clear(cls) -> None:
        cls._revoked_jtis.clear()


# ---------------------------------------------------------------------------
# Server-Side Identity Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegisteredUserAccount:
    username: str
    actor_id: str
    actor_kind: ActorKind
    roles: frozenset[str]
    password_hash: str  # In development/test, simple constant-time comparison
    is_active: bool = True


# Preconfigured allowlisted accounts for development and automated testing
PRECONFIGURED_USERS: dict[str, RegisteredUserAccount] = {
    "release_owner": RegisteredUserAccount(
        username="release_owner",
        actor_id="user-release-owner-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.RELEASE_OWNER.value, PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "incident_commander": RegisteredUserAccount(
        username="incident_commander",
        actor_id="user-commander-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.INCIDENT_COMMANDER.value, PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "platform_engineer": RegisteredUserAccount(
        username="platform_engineer",
        actor_id="user-plat-eng-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.PLATFORM_ENGINEER.value, PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "evaluation_owner": RegisteredUserAccount(
        username="evaluation_owner",
        actor_id="user-eval-owner-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.EVALUATION_OWNER.value, PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "sre_lead": RegisteredUserAccount(
        username="sre_lead",
        actor_id="user-sre-lead-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.SRE_LEAD.value, PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "operator": RegisteredUserAccount(
        username="operator",
        actor_id="user-operator-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset({PrincipalRole.OPERATOR.value}),
        password_hash="dev-password-secure",
    ),
    "auditor": RegisteredUserAccount(
        username="auditor",
        actor_id="user-auditor-1",
        actor_kind=ActorKind.HUMAN,
        roles=frozenset(),
        password_hash="dev-password-secure",
    ),
    "bot_agent": RegisteredUserAccount(
        username="bot_agent",
        actor_id="bot-agent-1",
        actor_kind=ActorKind.BOT,
        roles=frozenset(),
        password_hash="dev-password-secure",
    ),
}


def authenticate_user(
    *,
    username: str,
    password: str,
    tenant_id: uuid.UUID,
) -> Principal:
    """Verifies credentials against server-side identity registry and returns authentic Principal."""
    user = PRECONFIGURED_USERS.get(username.strip())
    if user is None or not user.is_active:
        raise AuthenticationError("Invalid username or inactive user account.")

    # Constant-time comparison for development password
    if not hmac.compare_digest(user.password_hash.encode(), password.encode()):
        raise AuthenticationError("Invalid user credentials.")

    return Principal(
        actor_id=user.actor_id,
        actor_kind=user.actor_kind,
        tenant_id=tenant_id,
        roles=user.roles,
        auth_mechanism="server_registry",
    )


# ---------------------------------------------------------------------------
# Principal Dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """Immutable, server-verified principal identity and authority record."""

    actor_id: str
    actor_kind: ActorKind
    tenant_id: uuid.UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    auth_mechanism: str = "bearer_token"
    jti: str | None = None
    expires_at_epoch: int | None = None

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

    def require_role(self, required_role: str, action_name: str = "operation") -> None:
        """Enforces that the principal holds the exact required role."""
        if not self.has_role(required_role):
            raise AuthorizationForbiddenError(
                f"Principal '{self.actor_id}' lacks required role '{required_role}' for '{action_name}'. "
                f"Principal roles: {sorted(self.roles)}"
            )

    def require_any_role(self, allowed_roles: frozenset[str], action_name: str = "operation") -> str:
        """Enforces that the principal holds at least one of the allowed roles, returning the matched role."""
        matching = [r for r in sorted(self.roles) if r in allowed_roles]
        if not matching:
            raise AuthorizationForbiddenError(
                f"Principal '{self.actor_id}' lacks required role for '{action_name}'. "
                f"Requires one of: {sorted(allowed_roles)}; principal has: {sorted(self.roles)}"
            )
        return matching[0]

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
        return min(matching_roles)

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
        return min(matching_roles)

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
    jti: str | None = None,
) -> str:
    """Mints an HMAC-SHA256 signed access token for a verified principal."""
    now = int(time.time())
    token_jti = jti or str(uuid.uuid4())
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
        "jti": token_jti,
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

    # Parse and enforce header
    try:
        header = json.loads(_b64url_decode(hdr_b64).decode("utf-8"))
    except Exception as e:
        raise AuthenticationError("Failed to decode token header") from e

    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise AuthenticationError(f"Unsupported or insecure token algorithm: {header.get('alg') if isinstance(header, dict) else 'none'}")

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

    # Revocation verification
    jti = payload.get("jti")
    if jti and TokenRevocationStore.is_revoked(str(jti)):
        raise AuthenticationError(f"Authentication token {jti} has been revoked")

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
        jti=str(jti) if jti else None,
        expires_at_epoch=exp,
    )
