"""OIDC boundary: validate signatures/claims and never trust tenant headers.

All tokens are synthetic RSA-signed fixtures; no external IdP is contacted.
"""

import time
from collections.abc import AsyncIterator
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from backend.api import app
from backend.auth import configured_identity, verify_bearer
from backend.database import get_session
from backend.settings import Settings, get_settings


ISSUER = "https://idp.example.test/"
AUDIENCE = "opsblaze-api"
AUTH_SETTINGS = Settings(
    environment="production",
    oidc_issuer=ISSUER,
    oidc_audience=AUDIENCE,
    oidc_jwks_url="https://idp.example.test/jwks",
)


@pytest.fixture
def signer(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    monkeypatch.setattr("backend.auth.signing_key", lambda token, settings: public)

    def issue(
        *,
        tenant: str = "alpha",
        subject: str = "operator-1",
        roles: tuple[str, ...] = ("operator",),
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        expires_in: int = 120,
        signing_private=None,
        algorithm: str = "RS256",
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "tenant_id": tenant,
            "roles": list(roles),
            "iat": now,
            "exp": now + expires_in,
        }
        return jwt.encode(
            claims, signing_private or private,
            algorithm=algorithm,
            headers={"kid": "fixture-1"},
        )

    return issue


@pytest.fixture
async def production_client(sessions_fixture) -> AsyncIterator[AsyncClient]:
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: AUTH_SETTINGS
    app.dependency_overrides[get_session] = session_override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def bearer(token: str, *, tenant: str = "alpha") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant}


def test_signed_claims_create_stable_pseudonym_not_raw_subject(signer):
    signed = signer(subject="sensitive@example.test")
    first = verify_bearer(signed, AUTH_SETTINGS)
    second = verify_bearer(signed, AUTH_SETTINGS)
    assert first == second
    assert first.tenant_id == "alpha"
    assert first.roles == frozenset({"operator"})
    assert "sensitive" not in first.subject
    assert first.subject.startswith("oidc:")


@pytest.mark.parametrize("override", [
    {"expires_in": -120},
    {"issuer": "https://impostor.example.test/"},
    {"audience": "wrong-service"},
    {"roles": ("root",)},
    {"roles": ()},
    {"tenant": "alpha/../beta"},
    {"subject": ""},
])
@pytest.mark.asyncio
async def test_invalid_claims_fail_closed(signer, production_client, override):
    token = signer(**override)
    response = await production_client.get("/v1/incidents", headers=bearer(token))
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_wrong_signer_and_algorithm_rejected(signer, production_client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_signer = signer(signing_private=other)
    rejected = await production_client.get(
        "/v1/incidents", headers=bearer(wrong_signer)
    )
    assert rejected.status_code == 401
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE}, "a-fixed-test-key-with-more-than-32-characters",
        algorithm="HS256", headers={"kid": "fixture-1"},
    )
    rejected_algorithm = await production_client.get(
        "/v1/incidents", headers=bearer(unsigned)
    )
    assert rejected_algorithm.status_code == 401


@pytest.mark.asyncio
async def test_verified_tenant_role_and_admission_actor(signer, production_client, sessions_fixture):
    operator = bearer(signer(roles=("operator",)))
    created = await production_client.post(
        "/v1/incidents",
        json={"target_id": "checkout"},
        headers={**operator, "Idempotency-Key": "oidc-production-incident"},
    )
    assert created.status_code == 202, created.text
    incident_id = created.json()["id"]
    assert (
        await production_client.get("/v1/incidents", headers=operator)
    ).status_code == 200
    from backend.models import Incident
    from uuid import UUID

    async with sessions_fixture() as session:
        row = await session.get(Incident, UUID(incident_id))
        assert row.created_by is not None and row.created_by.startswith("oidc:")

    forged = await production_client.get(
        f"/v1/incidents/{incident_id}",
        headers={**operator, "X-Tenant-Id": "beta"},
    )
    assert forged.status_code == 403
    other_tenant = await production_client.get(
        f"/v1/incidents/{incident_id}",
        headers=bearer(signer(tenant="beta"), tenant="beta"),
    )
    assert other_tenant.status_code == 404


@pytest.mark.asyncio
async def test_viewer_cannot_mutate_and_operator_cannot_approve(signer, production_client):
    viewer = bearer(signer(roles=("viewer",)))
    can_read = await production_client.get("/v1/incidents", headers=viewer)
    assert can_read.status_code == 200, can_read.text
    cannot_admit = await production_client.post(
        "/v1/incidents",
        headers={**viewer, "Idempotency-Key": "viewer"},
        json={"target_id": "checkout"},
    )
    assert cannot_admit.status_code == 403

    action = f"/v1/plans/{uuid4()}/decisions"
    deny = await production_client.post(
        action,
        headers={**bearer(signer()), "If-Match": "1"},
        json={"decision": "APPROVE"},
    )
    assert deny.status_code == 403
    reviewer = bearer(signer(subject="reviewer-2", roles=("reviewer",)))
    allowed = await production_client.post(
        action,
        headers={**reviewer, "If-Match": "1"},
        json={"decision": "APPROVE"},
    )
    assert allowed.status_code == 404, allowed.text
    reviewer_cannot_admit = await production_client.post(
        "/v1/incidents",
        headers={**reviewer, "Idempotency-Key": "reviewer"},
        json={"target_id": "checkout"},
    )
    assert reviewer_cannot_admit.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_headers_never_authenticate_production(signer, production_client):
    just_dev = await production_client.get(
        "/v1/incidents",
        headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "local-token"},
    )
    assert just_dev.status_code == 401
    mixed = await production_client.get(
        "/v1/incidents",
        headers={**bearer(signer()), "X-OpsBlaze-Token": "local-token"},
    )
    assert mixed.status_code == 401
    missing = await production_client.get("/v1/incidents")
    assert missing.status_code == 401


@pytest.mark.asyncio
async def test_missing_or_untrusted_configuration_fails_closed(signer, production_client):
    app.dependency_overrides[get_settings] = lambda: Settings(environment="production")
    assert (
        await production_client.get(
            "/v1/incidents", headers=bearer(signer())
        )
    ).status_code == 503
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url="http://127.0.0.1:8000/jwks",
    )
    assert (
        await production_client.get(
            "/v1/incidents", headers=bearer(signer())
        )
    ).status_code == 503
    assert configured_identity(AUTH_SETTINGS)
