"""Read-only G1 OIDC metadata preflight with no external HTTP or credentials."""

import base64
from copy import deepcopy

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.security.provider_preflight import verify_provider
from backend.settings import Settings

ISSUER = "https://idp.example.test/tenant"
JWKS = "https://idp.example.test/tenant/keys"


def b64(number):
    length = (number.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(
        number.to_bytes(length, "big"),
    ).decode().rstrip("=")


@pytest.fixture
def provider():
    public = rsa.generate_private_key(
        public_exponent=65537, key_size=2048,
    ).public_key().public_numbers()
    discovery = {
        "issuer": ISSUER,
        "jwks_uri": JWKS,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    keys = {
        "keys": [{
            "kid": "rotation-1", "kty": "RSA", "use": "sig", "alg": "RS256",
            "n": b64(public.n), "e": b64(public.e),
        }],
    }
    settings = Settings(
        environment="production",
        oidc_issuer=ISSUER,
        oidc_jwks_url=JWKS,
        oidc_audience="opsblaze-api",
    )
    return settings, discovery, keys


def client_for(discovery, keys, *, redirect=False, oversized=False, duplicate=False):
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            if redirect:
                return httpx.Response(302, headers={"Location": "https://elsewhere/"})
            if duplicate:
                return httpx.Response(200, text='{"issuer":"one","issuer":"two"}')
            if oversized:
                return httpx.Response(200, content=b"{" + b" " * 262_150 + b"}")
            return httpx.Response(200, json=discovery)
        if str(request.url) == JWKS:
            return httpx.Response(200, json=keys)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(route))


def test_live_provider_preflight_checks_pinned_discovery_and_real_rsa_metadata(provider):
    settings, discovery, keys = provider
    with client_for(discovery, keys) as client:
        result = verify_provider(settings, client)
    assert result["status"] == "PROVIDER_METADATA_VALID"
    assert result["usable_rs256_signing_keys"] == 1
    assert result["live_token_issuance_certified"] is False
    assert result["browser_pkce_flow_certified"] is False
    assert result["revocation_delivery_certified"] is False
    assert result["production_gate_passed"] is False
    assert "n" not in result and "kid" not in result


@pytest.mark.parametrize("change", [
    "issuer", "jwks_uri", "pkce", "code", "alg", "auth_host", "other_tenant", "token_http",
])
def test_changed_oidc_contract_fails_closed(provider, change):
    settings, discovery, keys = provider
    mutated = deepcopy(discovery)
    if change == "issuer":
        mutated["issuer"] = "https://attacker.example.test/"
    elif change == "jwks_uri":
        mutated["jwks_uri"] = "https://attacker.example.test/keys"
    elif change == "pkce":
        mutated["code_challenge_methods_supported"] = ["plain"]
    elif change == "code":
        mutated["response_types_supported"] = ["token"]
    elif change == "alg":
        mutated["id_token_signing_alg_values_supported"] = ["none"]
    elif change == "auth_host":
        mutated["authorization_endpoint"] = "https://attacker.example.test/a"
    elif change == "other_tenant":
        mutated["authorization_endpoint"] = "https://idp.example.test/other/auth"
    else:
        mutated["token_endpoint"] = "http://idp.example.test/token"
    with client_for(mutated, keys) as client:
        with pytest.raises(ValueError):
            verify_provider(settings, client)


@pytest.mark.parametrize("change", [
    "weak_modulus", "wrong_exponent", "missing_kid", "duplicate_kid",
    "wrong_alg", "not_rsa",
])
def test_weak_or_ambiguous_jwks_is_never_accepted(provider, change):
    settings, discovery, keys = provider
    keys = deepcopy(keys)
    key = keys["keys"][0]
    if change == "weak_modulus":
        key["n"] = b64(3 ** 100)
    elif change == "wrong_exponent":
        key["e"] = b64(3)
    elif change == "missing_kid":
        key.pop("kid")
    elif change == "duplicate_kid":
        keys["keys"].append(dict(key))
    elif change == "wrong_alg":
        key["alg"] = "HS256"
    else:
        key["kty"] = "EC"
    with client_for(discovery, keys) as client:
        with pytest.raises(ValueError):
            verify_provider(settings, client)


@pytest.mark.parametrize("bad_response", ["redirect", "oversized", "duplicate"])
def test_provider_preflight_does_not_follow_redirects_or_load_unbounded_data(
    provider, bad_response,
):
    settings, discovery, keys = provider
    with client_for(discovery, keys, **{bad_response: True}) as client:
        with pytest.raises(ValueError):
            verify_provider(settings, client)


def test_preflight_requires_explicit_production_same_origin_https(provider):
    settings, discovery, keys = provider
    with client_for(discovery, keys) as client:
        for options in (
            {"environment": "development"},
            {"oidc_jwks_url": "https://external.example.test/jwks"},
            {"oidc_jwks_url": "http://idp.example.test/jwks"},
            {"oidc_issuer": "https://idp.example.test/tenant?token=secret"},
        ):
            raw = {
                "environment": "production",
                "oidc_issuer": ISSUER,
                "oidc_jwks_url": JWKS,
                "oidc_audience": "opsblaze-api",
                **options,
            }
            with pytest.raises(ValueError):
                verify_provider(Settings(**raw), client)


def test_preflight_does_not_read_any_credentials_or_log_response(provider, caplog):
    settings, discovery, keys = provider
    with client_for(discovery, keys) as client:
        verify_provider(settings, client)
    assert "rotation-1" not in caplog.text
    assert keys["keys"][0]["n"] not in caplog.text


def test_https_default_port_is_same_origin_but_unrelated_ports_are_not():
    from backend.security.provider_preflight import _same_secure_origin

    issuer = "https://idp.example.test:443/tenant"
    assert _same_secure_origin(issuer, "https://idp.example.test/tenant/keys")
    assert not _same_secure_origin(
        issuer, "https://idp.example.test:9443/tenant/keys"
    )
    assert not _same_secure_origin(
        issuer, "https://idp.example.test/tenant/keys?other=1"
    )
