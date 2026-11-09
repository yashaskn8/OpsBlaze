"""Enterprise Identity Provider integration and RFC 7662 token introspection for OpsBlaze."""

from __future__ import annotations

import ipaddress
import socket
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from backend.config import Settings
from backend.domain.incident import DomainError


class IdentityIntrospectionError(DomainError):
    """Raised when upstream IdP introspection fails, returns inactive, or is invalid."""


IdentityProviderError = IdentityIntrospectionError


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
    upstream_token: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=16384,
        description="Raw bearer token issued by upstream IdP.",
    )
    access_token: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=16384,
        description="Alternative field name for upstream bearer token.",
    )
    ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
        description="Requested OpsBlaze session lifetime in seconds.",
    )

    @model_validator(mode="after")
    def validate_token_provided(self) -> TokenExchangeRequest:
        if not self.upstream_token and not self.access_token:
            raise ValueError("Either upstream_token or access_token must be provided.")
        return self

    @property
    def token_value(self) -> str:
        token = self.upstream_token or self.access_token
        return token.get_secret_value() if token else ""


class MembershipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: uuid.UUID
    idp_issuer: str = Field(min_length=1, max_length=256)
    idp_subject: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1, max_length=128)
    actor_kind: str = Field(default="HUMAN")
    roles: list[str] = Field(default_factory=list)


class MembershipDeactivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)


class TokenIntrospectionResult(BaseModel):
    """RFC 7662 Token Introspection Response Schema."""

    model_config = ConfigDict(extra="ignore")

    active: bool = Field(..., description="Whether the token is currently active.")
    scope: str | None = Field(default=None, description="OAuth 2.0 scopes granted.")
    client_id: str | None = Field(default=None, description="Client ID that requested the token.")
    sub: str | None = Field(default=None, description="Resource owner subject identifier.")
    username: str | None = Field(default=None, description="Human-readable resource owner username.")
    token_type: str | None = Field(default="Bearer", description="Type of token.")
    exp: int | None = Field(default=None, description="Integer timestamp indicating token expiry.")
    iat: int | None = Field(default=None, description="Integer timestamp indicating token issuance.")
    nbf: int | None = Field(default=None, description="Integer timestamp indicating not-before time.")
    aud: str | list[str] | None = Field(default=None, description="Audience(s) for the token.")
    iss: str | None = Field(default=None, description="Issuer URI of the token.")
    jti: str | None = Field(default=None, description="Unique JWT token identifier.")


def validate_introspection_url(url: str, is_production: bool = True) -> str:
    """Validates introspection URL against SSRF, loopback, private IP, and metadata attacks."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFBlockedError(f"Unsupported IdP protocol scheme: '{parsed.scheme}'. Must be HTTP/HTTPS.")
    if is_production and parsed.scheme.lower() != "https":
        raise SSRFBlockedError("Production IdP introspection endpoint MUST use HTTPS.")

    host = parsed.hostname
    if not host:
        raise SSRFBlockedError("Invalid URL: missing hostname.")

    host_lower = host.lower().rstrip(".")
    if host_lower in ("metadata.google.internal", "metadata", "instance-data", "localhost"):
        raise SSRFBlockedError(f"Host '{host}' is a forbidden cloud metadata or local address.")

    # Check if host is direct IP literal
    try:
        ip = ipaddress.ip_address(host_lower)
        if ip.is_loopback and is_production:
            raise SSRFBlockedError("Loopback IP addresses are forbidden in production.")
        if ip.is_link_local or ip.is_reserved or str(ip) == "169.254.169.254":
            raise SSRFBlockedError(f"IP address '{ip}' is a forbidden link-local or metadata address.")
        if is_production and ip.is_private:
            raise SSRFBlockedError(f"Private IP address '{ip}' is forbidden in production.")
    except ValueError:
        # Host is a DNS name; in production, verify resolved IPs to block SSRF / DNS rebinding
        if is_production:
            try:
                addr_info = socket.getaddrinfo(host_lower, None)
                for entry in addr_info:
                    sockaddr = entry[4]
                    ip_str = sockaddr[0]
                    ip = ipaddress.ip_address(ip_str)
                    if ip.is_loopback:
                        raise SSRFBlockedError(f"Host '{host}' resolves to forbidden loopback IP '{ip}'.")
                    if ip.is_link_local or ip.is_reserved or str(ip) == "169.254.169.254":
                        raise SSRFBlockedError(f"Host '{host}' resolves to forbidden metadata/link-local IP '{ip}'.")
                    if ip.is_private:
                        raise SSRFBlockedError(f"Host '{host}' resolves to forbidden private IP '{ip}'.")
            except (socket.gaierror, OSError) as exc:
                if isinstance(exc, SSRFBlockedError):
                    raise

    return url


class TokenIntrospectionClient:
    """Client for performing RFC 7662 OAuth 2.0 Token Introspection."""

    def __init__(
        self,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        expected_issuer: str | None = None,
        expected_audience: str | None = None,
        timeout_seconds: float = 5.0,
        is_production: bool = True,
    ) -> None:
        self._raw_url = introspection_url
        self._url = validate_introspection_url(introspection_url, is_production=is_production)
        self._client_id = client_id
        self._client_secret = client_secret
        self._expected_issuer = expected_issuer
        self._expected_audience = expected_audience
        self._timeout_seconds = timeout_seconds
        self._is_production = is_production

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenIntrospectionClient:
        introspection_url = settings.idp_introspection_url or settings.identity_introspection_url
        client_id = settings.idp_client_id or settings.identity_client_id
        client_secret = settings.idp_client_secret or settings.identity_client_secret
        if not introspection_url or not client_id or not client_secret:
            raise IdentityIntrospectionError(
                "IdP introspection is not configured. Missing introspection URL, client ID, or secret."
            )
        is_prod = settings.opsblaze_env.strip().lower() == "production"
        expected_iss = settings.idp_expected_issuer or settings.identity_issuer
        expected_aud = settings.idp_expected_audience or settings.identity_audience
        return cls(
            introspection_url=introspection_url,
            client_id=client_id,
            client_secret=client_secret,
            expected_issuer=expected_iss,
            expected_audience=expected_aud,
            timeout_seconds=settings.idp_timeout_seconds,
            is_production=is_prod,
        )

    async def introspect_token(self, token: str) -> TokenIntrospectionResult:
        """Sends an RFC 7662 POST request to the introspection endpoint."""
        if not token or not token.strip():
            raise IdentityIntrospectionError("Cannot introspect empty token.")
        if len(token) > 16384:
            raise IdentityIntrospectionError("Token exceeds maximum length limit of 16384 characters.")

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "OpsBlaze-IdentityIntrospection/1.0",
        }
        data = {
            "token": token,
            "token_type_hint": "access_token",
        }
        auth = (self._client_id, self._client_secret)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout=self._timeout_seconds,
                    connect=min(2.0, self._timeout_seconds),
                    read=min(3.0, self._timeout_seconds),
                ),
                follow_redirects=False,
            ) as client:
                resp = await client.post(
                    self._url,
                    data=data,
                    headers=headers,
                    auth=auth,
                )
        except httpx.TimeoutException as exc:
            raise IdentityIntrospectionError(f"IdP introspection timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise IdentityIntrospectionError(f"IdP introspection connection error: {exc}") from exc

        if resp.status_code != 200:
            raise IdentityIntrospectionError(
                f"IdP introspection returned HTTP {resp.status_code}: {resp.text[:256]}"
            )

        content_length = len(resp.content)
        if content_length > 32 * 1024:
            raise IdentityIntrospectionError(
                f"IdP response size ({content_length} bytes) exceeds maximum limit (32KB)."
            )

        try:
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            raise IdentityIntrospectionError(f"IdP response is not valid JSON: {exc}") from exc

        try:
            result = TokenIntrospectionResult.model_validate(payload)
        except Exception as exc:
            raise IdentityIntrospectionError(f"Malformed RFC 7662 introspection response: {exc}") from exc

        return self.validate_claims(result)

    def validate_claims(self, result: TokenIntrospectionResult) -> TokenIntrospectionResult:
        """Validates standard RFC 7662 assertions."""
        # 1. Active check
        if not result.active:
            raise IdentityIntrospectionError("Upstream IdP returned token is inactive or revoked.")

        # 2. Issuer check
        if self._expected_issuer:
            expected_iss = self._expected_issuer.rstrip("/")
            if not result.iss or result.iss.rstrip("/") != expected_iss:
                raise IdentityIntrospectionError(
                    f"Token issuer mismatch. Expected '{expected_iss}', got '{result.iss}'."
                )

        # 3. Audience check
        if self._expected_audience:
            expected_aud = self._expected_audience
            if isinstance(result.aud, str):
                if result.aud != expected_aud:
                    raise IdentityIntrospectionError(
                        f"Token audience mismatch. Expected '{expected_aud}', got '{result.aud}'."
                    )
            elif isinstance(result.aud, list):
                if expected_aud not in result.aud:
                    raise IdentityIntrospectionError(
                        f"Expected audience '{expected_aud}' not present in token audiences: {result.aud}."
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


async def introspect_access_token(
    access_token: str,
    *,
    issuer: str,
    introspection_url: str,
    client_id: str,
    client_secret: str,
    audience: str,
) -> dict[str, Any]:
    """Ask the configured issuer to authenticate an access token, then validate identity claims."""
    client = TokenIntrospectionClient(
        introspection_url=introspection_url,
        client_id=client_id,
        client_secret=client_secret,
        expected_issuer=issuer,
        expected_audience=audience,
        is_production=introspection_url.startswith("https://"),
    )
    result = await client.introspect_token(access_token)
    return result.model_dump()
