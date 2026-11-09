"""Enterprise Identity Provider integration and RFC 7662 token introspection for OpsBlaze."""

from __future__ import annotations

import ipaddress
import socket
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.config import Settings
from backend.domain.incident import DomainError


class IdentityIntrospectionError(DomainError):
    """Raised when upstream IdP introspection fails, returns inactive, or is invalid."""


class IdentityMembershipNotFoundError(DomainError):
    """Raised when an authenticated IdP subject has no registered tenant membership."""


class IdentityMembershipDeactivatedError(DomainError):
    """Raised when an IdP subject's tenant membership has been deactivated."""


class SSRFBlockedError(DomainError):
    """Raised when an IdP URL points to loopback, metadata, or forbidden internal addresses."""


class TokenExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: uuid.UUID = Field(
        ...,
        description="Target tenant ID for session exchange.",
    )
    upstream_token: SecretStr = Field(
        ...,
        min_length=16,
        max_length=8192,
        description="Raw bearer token issued by upstream IdP.",
    )
    ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Requested OpsBlaze session lifetime in seconds.",
    )


class TokenIntrospectionResult(BaseModel):
    """RFC 7662 Token Introspection Response Schema."""

    model_config = ConfigDict(extra="ignore")

    active: bool = Field(
        ..., description="Boolean indicator of whether or not the token is currently active."
    )
    sub: str | None = Field(default=None, description="Subject identifier.")
    iss: str | None = Field(default=None, description="Issuer URI.")
    aud: str | list[str] | None = Field(default=None, description="Intended audience.")
    exp: int | None = Field(default=None, description="Expiration time (Unix seconds).")
    iat: int | None = Field(default=None, description="Issue time (Unix seconds).")
    nbf: int | None = Field(default=None, description="Not-before time (Unix seconds).")
    client_id: str | None = None
    scope: str | None = None
    username: str | None = None
    token_type: str | None = None
    jti: str | None = None


class TokenExchangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    principal: dict[str, Any]


class MembershipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: uuid.UUID
    idp_issuer: str = Field(min_length=1, max_length=256)
    idp_subject: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: str = Field(default="HUMAN")
    roles: list[str] = Field(min_length=1)


class MembershipDeactivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)


BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "localhost",
    }
)


def validate_introspection_url(url: str, is_production: bool = False) -> None:
    """Validates an introspection URL against SSRF and transport security constraints."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        if is_production:
            raise SSRFBlockedError(
                f"Production introspection URL must use HTTPS scheme; got '{parsed.scheme}'."
            )
        if parsed.scheme != "http":
            raise SSRFBlockedError(
                f"Introspection URL must use HTTP or HTTPS scheme; got '{parsed.scheme}'."
            )

    host = parsed.hostname
    if not host:
        raise SSRFBlockedError(f"Introspection URL '{url}' has no valid hostname.")

    if host.lower() in BLOCKED_HOSTNAMES:
        raise SSRFBlockedError(f"Introspection hostname '{host}' is forbidden (SSRF risk).")

    # IP address evaluation
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            if is_production:
                raise SSRFBlockedError(f"Loopback IP '{ip}' forbidden in production.")
        elif ip.is_link_local or ip.is_reserved or str(ip) == "169.254.169.254":
            raise SSRFBlockedError(f"Link-local or metadata IP '{ip}' forbidden (SSRF risk).")
    except ValueError:
        # Not a raw IP literal; hostname resolution check in production
        if is_production:
            try:
                resolved_ips = socket.getaddrinfo(host, None)
                for family, _, _, _, sockaddr in resolved_ips:
                    addr_str = sockaddr[0]
                    resolved_ip = ipaddress.ip_address(addr_str)
                    if (
                        resolved_ip.is_loopback
                        or resolved_ip.is_link_local
                        or str(resolved_ip) == "169.254.169.254"
                    ):
                        raise SSRFBlockedError(
                            f"Hostname '{host}' resolves to forbidden address '{addr_str}'."
                        )
            except (socket.gaierror, OSError) as exc:
                raise SSRFBlockedError(f"Cannot resolve IdP hostname '{host}': {exc}") from exc


class TokenIntrospectionClient:
    """Hardened RFC 7662 OAuth 2.0 token introspection client."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._is_production = settings.opsblaze_env.strip().lower() == "production"
        self._client = http_client

    async def introspect_token(self, token: SecretStr) -> TokenIntrospectionResult:
        """Introspects an upstream OAuth 2.0 token per RFC 7662."""
        raw_token = token.get_secret_value()
        if not raw_token or len(raw_token.strip()) < 8:
            raise IdentityIntrospectionError("Upstream token is empty or invalid.")

        url = self._settings.idp_introspection_url
        if not url:
            raise IdentityIntrospectionError(
                "IdP introspection URL is not configured on this server."
            )

        validate_introspection_url(url, is_production=self._is_production)

        client_id = self._settings.idp_client_id
        client_secret = self._settings.idp_client_secret
        auth: tuple[str, str] | None = None
        if client_id and client_secret:
            auth = (client_id, client_secret)

        timeout = httpx.Timeout(
            timeout=self._settings.idp_timeout_seconds,
            connect=2.0,
            read=3.0,
        )

        data = {"token": raw_token, "token_type_hint": "access_token"}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        post_kwargs: dict[str, Any] = {
            "data": data,
            "headers": headers,
            "timeout": timeout,
        }
        if auth is not None:
            post_kwargs["auth"] = auth

        # Dedicated or caller-provided AsyncClient
        if self._client is not None:
            resp = await self._client.post(
                url,
                follow_redirects=False,
                **post_kwargs,
            )
        else:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                resp = await client.post(
                    url,
                    **post_kwargs,
                )

        if resp.status_code != 200:
            raise IdentityIntrospectionError(
                f"Upstream IdP returned unexpected status code {resp.status_code}."
            )

        # Max payload length cap (32 KiB)
        if len(resp.content) > 32 * 1024:
            raise IdentityIntrospectionError("Upstream IdP response exceeds 32 KiB limit.")

        try:
            payload = resp.json()
        except ValueError as json_err:
            raise IdentityIntrospectionError(f"Malformed JSON from IdP: {json_err}") from json_err

        result = TokenIntrospectionResult.model_validate(payload)

        # 1. Active status check
        if not result.active:
            raise IdentityIntrospectionError("Upstream token is inactive or revoked.")

        # 2. Issuer check
        if self._settings.idp_expected_issuer and (
            not result.iss or result.iss.strip() != self._settings.idp_expected_issuer.strip()
        ):
            raise IdentityIntrospectionError(
                f"Token issuer '{result.iss}' does not match expected '{self._settings.idp_expected_issuer}'."
            )

        # 3. Audience check
        if self._settings.idp_expected_audience:
            expected_aud = self._settings.idp_expected_audience.strip()
            if isinstance(result.aud, list):
                if expected_aud not in result.aud:
                    raise IdentityIntrospectionError(
                        f"Expected audience '{expected_aud}' not present in token audiences."
                    )
            elif isinstance(result.aud, str):
                if result.aud.strip() != expected_aud:
                    raise IdentityIntrospectionError(
                        f"Token audience '{result.aud}' does not match expected '{expected_aud}'."
                    )
            else:
                raise IdentityIntrospectionError(
                    f"Token audience missing; expected '{expected_aud}'."
                )

        # 4. Expiry check
        if result.exp is not None:
            now_sec = int(time.time())
            if result.exp <= now_sec:
                raise IdentityIntrospectionError(
                    f"Upstream token expired at {result.exp} (now: {now_sec})."
                )

        # 5. Subject check
        if not result.sub or not result.sub.strip():
            raise IdentityIntrospectionError("Upstream token contains no subject ('sub').")

        return result
