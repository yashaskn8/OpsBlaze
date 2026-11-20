"""Pinned JWKS key selection and rotation without contacting a real IdP."""

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient
from jwt.algorithms import RSAAlgorithm

from backend.auth import jwks_client, verify_bearer
from backend.security.idp_events import verify_identity_event
from backend.settings import Settings

SETTINGS = Settings(
    environment="production",
    oidc_issuer="https://idp.example.test/",
    oidc_audience="opsblaze-api",
    oidc_jwks_url="https://idp.example.test/jwks",
    idp_event_audience="opsblaze-security-events",
)


def test_pinned_jwks_refreshes_on_new_key_id_and_rejects_forged_kid(monkeypatch):
    private_one = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_two = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = {"current": 1, "lookups": []}

    def key_payload(key, kid):
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}

    def fake_jwks(self):
        # No token header can change the pinned network endpoint.
        state["lookups"].append(self.uri)
        assert self.uri == SETTINGS.oidc_jwks_url
        key = private_one if state["current"] == 1 else private_two
        return {"keys": [key_payload(key, f"rotation-{state['current']}")]}

    monkeypatch.setattr(PyJWKClient, "fetch_data", fake_jwks)
    jwks_client.cache_clear()
    try:
        now = int(time.time())

        def signed(private, kid, audience, **extra):
            payload = {
                "iss": SETTINGS.oidc_issuer,
                "aud": audience,
                "sub": "operator-test",
                "tenant_id": "alpha",
                "roles": ["operator"],
                "iat": now,
                "exp": now + 120,
                **extra,
            }
            return jwt.encode(
                payload, private, algorithm="RS256",
                headers={"kid": kid, "jku": "https://impostor.example.test/jwks"},
            )

        first = signed(private_one, "rotation-1", SETTINGS.oidc_audience)
        assert verify_bearer(first, SETTINGS).tenant_id == "alpha"

        # A new kid forces refresh instead of reusing a stale signing key.
        state["current"] = 2
        second = signed(private_two, "rotation-2", SETTINGS.oidc_audience)
        assert verify_bearer(second, SETTINGS).roles == frozenset({"operator"})
        assert len(state["lookups"]) >= 2

        event = signed(
            private_two, "rotation-2", SETTINGS.idp_event_audience,
            event_type="IDENTITY_ACCESS_REVOKED", jti=uuid4().hex,
        )
        assert verify_identity_event(event, SETTINGS).tenant_id == "alpha"

        bad = signed(private_one, "rotation-2", SETTINGS.oidc_audience)
        with pytest.raises(jwt.PyJWTError):
            verify_bearer(bad, SETTINGS)
        assert set(state["lookups"]) == {SETTINGS.oidc_jwks_url}
    finally:
        jwks_client.cache_clear()
