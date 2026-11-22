"""G1 read-only live OIDC provider metadata preflight.

An operator supplies a real, authorized issuer/JWKS through OPSBLAZE_ env
settings. This does not sign in users, use credentials, or certify G1.
"""

import base64
import hashlib
import json
from urllib.parse import urlparse

import httpx

from backend.auth import configured_identity
from backend.settings import Settings

MAX_DOCUMENT_BYTES = 262_144


def _json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate provider metadata key")
        result[key] = value
    return result


def _reject_number(_value: str):
    raise ValueError("unsupported provider numeric literal")


def _get_json(client: httpx.Client, url: str) -> dict:
    # Read a bounded stream; redirects must not circumvent pinned endpoints.
    with client.stream(
        "GET", url, headers={"Accept": "application/json"},
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise ValueError("provider endpoint did not return HTTP 200")
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > MAX_DOCUMENT_BYTES:
                raise ValueError("provider metadata exceeds fixed size bound")
            body.extend(chunk)
    data = json.loads(
        body, object_pairs_hook=_json_pairs, parse_constant=_reject_number,
    )
    if not isinstance(data, dict):
        raise ValueError("provider metadata must be a JSON object")
    return data


def _same_secure_origin(a: str, b: str) -> bool:
    first, second = urlparse(a), urlparse(b)
    return (
        first.scheme == second.scheme == "https"
        and bool(first.hostname and second.hostname)
        and first.hostname == second.hostname
        and (first.port or 443) == (second.port or 443)
        and not any((
            first.username, first.password, first.params, first.fragment, first.query,
            second.username, second.password, second.params, second.fragment, second.query,
        ))
    )


def _usable_rsa_keys(jwks: dict) -> int:
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 50:
        raise ValueError("JWKS has no bounded signing key set")
    accepted = set()
    for key in keys:
        if not isinstance(key, dict):
            raise ValueError("invalid JWKS key record")
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
        ):
            continue
        kid, n, exponent = (
            key.get("kid"), key.get("n"), key.get("e"),
        )
        if (
            not isinstance(kid, str)
            or not 1 <= len(kid) <= 128
            or not kid.isascii()
            or any(not 33 <= ord(ch) <= 126 for ch in kid)
            or kid in accepted
            or not isinstance(n, str)
            or not 342 <= len(n) <= 1366
            or not isinstance(exponent, str)
        ):
            raise ValueError("invalid RSA signing key identity")
        try:
            raw_n = base64.b64decode(n + "=" * (-len(n) % 4),
                                     altchars=b"-_", validate=True)
            raw_e = base64.b64decode(
                exponent + "=" * (-len(exponent) % 4),
                altchars=b"-_", validate=True,
            )
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("invalid RSA key encoding") from exc
        if (
            base64.urlsafe_b64encode(raw_n).decode().rstrip("=") != n
            or base64.urlsafe_b64encode(raw_e).decode().rstrip("=") != exponent
            or not 2048 <= int.from_bytes(raw_n, "big").bit_length() <= 8192
            or int.from_bytes(raw_e, "big") != 65537
        ):
            raise ValueError("weak or noncanonical provider RSA key")
        accepted.add(kid)
    if not accepted:
        raise ValueError("no RS256 signing keys satisfy minimum strength")
    return len(accepted)


def verify_provider(settings: Settings, client: httpx.Client) -> dict:
    """Pass only if endpoints and live read-only signing metadata match pins."""
    if (
        settings.environment != "production"
        or not configured_identity(settings)
        or not _same_secure_origin(
            settings.oidc_issuer, settings.oidc_jwks_url,
        )
    ):
        raise ValueError("production OIDC endpoints must share a pinned HTTPS origin")
    discovery_url = settings.oidc_issuer.rstrip(
        "/"
    ) + "/.well-known/openid-configuration"
    discovery = _get_json(client, discovery_url)
    if (
        discovery.get("issuer") != settings.oidc_issuer
        or discovery.get("jwks_uri") != settings.oidc_jwks_url
        or not isinstance(discovery.get("response_types_supported"), list)
        or "code" not in discovery["response_types_supported"]
        or not isinstance(discovery.get("code_challenge_methods_supported"), list)
        or "S256" not in discovery["code_challenge_methods_supported"]
        or not isinstance(discovery.get("id_token_signing_alg_values_supported"), list)
        or "RS256" not in discovery["id_token_signing_alg_values_supported"]
        or any(
            not isinstance(discovery.get(field), str)
            or not _same_secure_origin(
                settings.oidc_issuer, discovery[field],
            )
            or not urlparse(discovery[field]).path.startswith(
                urlparse(settings.oidc_issuer).path.rstrip("/") + "/"
            )
            for field in ("authorization_endpoint", "token_endpoint")
        )
    ):
        raise ValueError("provider discovery does not satisfy pinned OIDC contract")
    keys = _usable_rsa_keys(_get_json(client, settings.oidc_jwks_url))
    return {
        "status": "PROVIDER_METADATA_VALID",
        "issuer": settings.oidc_issuer,
        "jwks_url_sha256": hashlib.sha256(
            settings.oidc_jwks_url.encode("utf-8")
        ).hexdigest(),
        "usable_rs256_signing_keys": keys,
        "live_token_issuance_certified": False,
        "browser_pkce_flow_certified": False,
        "revocation_delivery_certified": False,
        "independent_security_review_certified": False,
        "production_gate_passed": False,
    }


def main() -> None:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            result = verify_provider(Settings(), client)
    except (httpx.HTTPError, ValueError, OSError, json.JSONDecodeError):
        result = {
            "status": "INVALID",
            "production_gate_passed": False,
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "INVALID":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
