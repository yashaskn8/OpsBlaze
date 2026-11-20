"""Production identity-provider security event contract.

Synthetic RSA fixtures test signature, scope, replay, and grant revocation.
An actual external identity provider still needs integration certification.
"""

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import backend.auth as auth
from backend.api import app
from backend.database import get_session
from backend.models import (
    ApprovalGrant, IdentityProviderEvent, IncidentPlan, RevokedIdentity,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings

ISSUER = "https://idp.example.test/"
EVENT_AUDIENCE = "opsblaze-security-events"
SETTINGS = Settings(
    environment="production",
    oidc_issuer=ISSUER,
    oidc_audience="opsblaze-api",
    oidc_jwks_url="https://idp.example.test/jwks",
    idp_event_audience=EVENT_AUDIENCE,
)


@pytest.fixture
def issue(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        auth, "signing_key", lambda _token, _settings: private.public_key(),
    )

    def mint(*, subject="reviewer-1", tenant="alpha",
             audience=EVENT_AUDIENCE, event_type="IDENTITY_ACCESS_REVOKED",
             jti=None, issued_at=None, expires_at=None, roles=None):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": audience,
            "sub": subject,
            "tenant_id": tenant,
            "iat": now if issued_at is None else issued_at,
            "exp": now + 120 if expires_at is None else expires_at,
            "jti": uuid4().hex if jti is None else jti,
            "event_type": event_type,
        }
        if roles is not None:
            claims.pop("event_type")
            claims["roles"] = list(roles)
        return jwt.encode(
            claims, private, algorithm="RS256", headers={"kid": "fixture"},
        )
    return mint


@pytest.fixture
async def client(sessions_fixture):
    async def db_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_session] = db_override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test",
        ) as api_client:
            yield api_client
    finally:
        app.dependency_overrides.clear()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_claims", [
    {"audience": "opsblaze-api"},
    {"audience": "wrong-audience"},
    {"event_type": "IDENTITY_ROLE_ADDED"},
    {"issued_at": 1, "expires_at": 30},
    {"tenant": "alpha/../../beta"},
    {"jti": "too-short"},
    {"subject": ""},
])
async def test_idp_event_rejects_untrusted_claims(issue, client, bad_claims):
    response = await client.post(
        "/v1/security/idp-events",
        headers=headers(issue(**bad_claims)),
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_idp_revocation_is_idempotent_and_revokes_pending_grant(
    issue, client, sessions_fixture,
):
    token = issue(subject="reviewer-1")
    reviewer = auth.verify_bearer(
        issue(subject="reviewer-1", audience="opsblaze-api", roles=("reviewer",)),
        SETTINGS,
    )
    plan_id, grant_id = uuid4(), uuid4()
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "provider-event",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(IncidentPlan(
                id=plan_id, tenant_id="alpha", incident_id=incident.id,
                target_id="checkout", action_id="SIMULATOR_MARK_HEALTHY",
                plan_digest="d" * 64, expected_version=1,
                evidence_ids=[], status="APPROVED", version=2,
                decided_by=reviewer.subject, review_epoch=1,
            ))
            await session.flush()
            session.add(ApprovalGrant(
                id=grant_id, tenant_id="alpha", plan_id=plan_id,
                plan_digest="d" * 64, reviewer_id=reviewer.subject,
                scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                state="APPROVED", version=1,
                reason="trusted independent simulator reviewer",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ))
    accepted = await client.post("/v1/security/idp-events", headers=headers(token))
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["grants_revoked"] == 1

    again = await client.post("/v1/security/idp-events", headers=headers(token))
    assert again.status_code == 202
    assert again.json()["grants_revoked"] == 0

    async with sessions_fixture() as session:
        revoked = await session.get(RevokedIdentity, ("alpha", reviewer.subject))
        grant = await session.get(ApprovalGrant, grant_id)
        plan = await session.get(IncidentPlan, plan_id)
        events = (await session.scalars(select(IdentityProviderEvent))).all()
        assert revoked is not None and revoked.revoked_by == "IDP_EVENT"
        assert grant.state == "REVOKED" and grant.version == 2
        assert plan.status == "REVOKED" and plan.version == 3
        assert len(events) == 1
        assert events[0].tenant_id == "alpha"
        assert events[0].actor_id == reviewer.subject
        assert "reviewer-1" not in str(events[0].__dict__)

    access_token = issue(
        subject="reviewer-1", audience="opsblaze-api", roles=("reviewer",),
    )
    assert (
        await client.get("/v1/auth/me", headers=headers(access_token))
    ).status_code == 403
    # Revocation is tenant-scoped, even for the same IdP subject.
    other_tenant = issue(
        subject="reviewer-1", tenant="beta",
        audience="opsblaze-api", roles=("reviewer",),
    )
    assert (
        await client.get("/v1/auth/me", headers=headers(other_tenant))
    ).status_code == 200


@pytest.mark.asyncio
async def test_idp_event_is_not_regular_user_access_token(issue, client):
    access = issue(audience="opsblaze-api", roles=("security_admin",))
    rejected = await client.post(
        "/v1/security/idp-events", headers=headers(access),
    )
    assert rejected.status_code == 401
    assert (await client.post("/v1/security/idp-events")).status_code == 401


@pytest.mark.asyncio
async def test_idp_event_endpoint_is_closed_without_independent_audience(
    issue, client,
):
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production", oidc_issuer=ISSUER,
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/jwks",
    )
    assert (
        await client.post(
            "/v1/security/idp-events", headers=headers(issue()),
        )
    ).status_code == 503

    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="development", local_token="dev-only",
        idp_event_audience=EVENT_AUDIENCE,
    )
    assert (
        await client.post(
            "/v1/security/idp-events", headers=headers(issue()),
        )
    ).status_code == 503
