"""Trusted OAuth2 token introspection for production identity exchange."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.parse
import urllib.request
from typing import Any


class IdentityProviderError(ValueError):
    """Raised when an upstream identity token cannot be verified."""


def _introspect_sync(
    access_token: str,
    *,
    issuer: str,
    introspection_url: str,
    client_id: str,
    client_secret: str,
    audience: str,
) -> dict[str, Any]:
    if not access_token or len(access_token) > 16_384:
        raise IdentityProviderError("Missing or oversized upstream access token.")
    if not introspection_url.startswith("https://") or not issuer.startswith("https://"):
        raise IdentityProviderError("Identity issuer and introspection URL must use HTTPS.")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode(
        {"token": access_token, "token_type_hint": "access_token"}
    ).encode("ascii")
    request = urllib.request.Request(
        introspection_url,
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "OpsBlaze-Identity/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise IdentityProviderError("Identity provider introspection failed.")
            raw = response.read(64 * 1024 + 1)
    except OSError as exc:
        raise IdentityProviderError("Identity provider is unavailable.") from exc
    if len(raw) > 64 * 1024:
        raise IdentityProviderError("Identity provider response exceeds the size limit.")
    try:
        claims = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IdentityProviderError("Identity provider returned malformed introspection JSON.") from exc
    if not isinstance(claims, dict) or claims.get("active") is not True:
        raise IdentityProviderError("Upstream access token is inactive or invalid.")

    if claims.get("iss") != issuer.rstrip("/"):
        raise IdentityProviderError("Identity provider issuer does not match configured issuer.")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud
    if not isinstance(audiences, list) or audience not in audiences:
        raise IdentityProviderError("Upstream access token is not intended for OpsBlaze.")
    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)) or int(expiry) <= int(time.time()):
        raise IdentityProviderError("Upstream access token is expired or missing expiry.")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise IdentityProviderError("Upstream access token has no valid subject.")
    return claims


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
    return await asyncio.to_thread(
        _introspect_sync,
        access_token,
        issuer=issuer,
        introspection_url=introspection_url,
        client_id=client_id,
        client_secret=client_secret,
        audience=audience,
    )
