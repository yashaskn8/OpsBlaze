"""OIDC boundary: validate signatures/claims and never trust tenant headers.

All tokens are synthetic RSA-signed fixtures; no external IdP is contacted.
"""

import time
from datetime import datetime, timedelta, timezone
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
        jwt_headers: dict | None = None,
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
            headers={"kid": "fixture-1", **(jwt_headers or {})},
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
    {"expires_in": 7200},
    {"audience": [AUDIENCE, "another-api"]},
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


@pytest.mark.asyncio
async def test_authenticated_identity_returns_verified_roles_without_raw_sub(
    signer, production_client,
):
    token = signer(tenant="alpha", subject="name@example.test", roles=("reviewer", "viewer"))
    response = await production_client.get(
        "/v1/auth/me", headers=bearer(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "alpha"
    assert body["roles"] == ["reviewer", "viewer"]
    assert body["actor_id"].startswith("oidc:")
    assert "name@example.test" not in str(body)
    forged = await production_client.get(
        "/v1/auth/me", headers=bearer(token, tenant="beta"),
    )
    assert forged.status_code == 403


@pytest.mark.asyncio
async def test_security_admin_revokes_identity_and_all_its_pending_grants(
    signer, production_client, sessions_fixture,
):
    from backend.models import (
        ApprovalGrant, ApprovalRevocation, IncidentPlan, RevokedIdentity,
    )
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident
    from sqlalchemy import select
    from uuid import uuid4

    tenant = "alpha"
    reviewer_token = signer(subject="reviewer-to-revoke", roles=("reviewer",))
    reviewer_id = verify_bearer(reviewer_token, AUTH_SETTINGS).subject
    admin_token = signer(subject="security-admin", roles=("security_admin",))
    admin_id = verify_bearer(admin_token, AUTH_SETTINGS).subject
    incident_id, plan_id, grant_id = None, uuid4(), uuid4()
    async with sessions_fixture() as session:
        admitted = await admit_incident(
            session, tenant, "security-revoke",
            IncidentCreate(target_id="checkout"),
        )
        incident_id = admitted.id
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(IncidentPlan(
                id=plan_id, tenant_id=tenant, incident_id=incident_id,
                target_id="checkout", action_id="SIMULATOR_MARK_HEALTHY",
                plan_digest="a" * 64, expected_version=1,
                evidence_ids=[], status="APPROVED", version=2,
                decided_by=reviewer_id, review_epoch=1,
            ))
            await session.flush()
            session.add(ApprovalGrant(
                id=grant_id, tenant_id=tenant, plan_id=plan_id,
                plan_digest="a" * 64, reviewer_id=reviewer_id,
                scope="SIMULATOR_MARK_HEALTHY",
                decision="APPROVE", state="APPROVED",
                reason="approved isolated simulator fixture",
                version=1, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ))

    route = f"/v1/security/identities/{reviewer_id}/revoke"
    denied = await production_client.post(
        route, headers=bearer(reviewer_token),
        json={"reason": "reviewer access withdrawn"},
    )
    assert denied.status_code == 403

    forbidden_multi_role = await production_client.post(
        route,
        headers=bearer(signer(
            subject="both-admin-reviewer", roles=("security_admin", "reviewer"),
        )),
        json={"reason": "reviewer access withdrawn"},
    )
    assert forbidden_multi_role.status_code == 403

    revoked = await production_client.post(
        route, headers=bearer(admin_token),
        json={"reason": "reviewer permissions withdrawn by security authority"},
    )
    assert revoked.status_code == 202, revoked.text
    assert revoked.json()["grants_revoked"] == 1
    assert (
        await production_client.get("/v1/auth/me", headers=bearer(reviewer_token))
    ).status_code == 403
    assert (
        await production_client.get("/v1/auth/me", headers=bearer(admin_token))
    ).status_code == 200

    # Revocation is tenant-specific even when issuer + subject are identical.
    beta_reviewer = signer(
        tenant="beta", subject="reviewer-to-revoke", roles=("reviewer",),
    )
    assert (
        await production_client.get(
            "/v1/auth/me", headers=bearer(beta_reviewer, tenant="beta"),
        )
    ).status_code == 200

    again = await production_client.post(
        route, headers=bearer(admin_token),
        json={"reason": "duplicate operator message with same identity"},
    )
    assert again.status_code == 202
    assert again.json()["grants_revoked"] == 0

    async with sessions_fixture() as session:
        record = await session.get(RevokedIdentity, (tenant, reviewer_id))
        grant = await session.get(ApprovalGrant, grant_id)
        plan = await session.get(IncidentPlan, plan_id)
        audits = (await session.scalars(select(ApprovalRevocation).where(
            ApprovalRevocation.approval_id == grant_id
        ))).all()
        assert record.revoked_by == admin_id
        assert grant.state == "REVOKED" and grant.version == 2
        assert plan.status == "REVOKED" and plan.version == 3
        assert len(audits) == 1 and audits[0].revoked_by == admin_id


@pytest.mark.asyncio
async def test_development_token_cannot_claim_security_admin(signer, production_client):
    actor_id = verify_bearer(signer(), AUTH_SETTINGS).subject
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="development", local_token="dev-key",
    )
    response = await production_client.post(
        f"/v1/security/identities/{actor_id}/revoke",
        json={"reason": "non-production attempt to revoke"},
        headers={"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "dev-key"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_redteam_dual_operator_reviewer_role_cannot_approve(
    signer, production_client,
):
    both = bearer(signer(roles=("operator", "reviewer")))
    # A reviewer must not share operator authority, even for another
    # operator's incident or a nonexistent plan.
    denied = await production_client.post(
        f"/v1/plans/{uuid4()}/decisions",
        headers={**both, "If-Match": "1"},
        json={"decision": "APPROVE"},
    )
    assert denied.status_code == 403

    only_reviewer = bearer(signer(roles=("reviewer",)))
    permitted_to_reach_plan_lookup = await production_client.post(
        f"/v1/plans/{uuid4()}/decisions",
        headers={**only_reviewer, "If-Match": "1"},
        json={"decision": "APPROVE"},
    )
    assert permitted_to_reach_plan_lookup.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("jwt_headers", [
    {"crit": ["unknown-extension"], "unknown-extension": "required"},
    {"kid": "k" * 129},
    {"kid": ""},
    {"kid": "invalid\nkey"},
])
async def test_redteam_signed_access_token_rejects_unsupported_header(
    signer, production_client, jwt_headers,
):
    token = signer(jwt_headers=jwt_headers)
    rejected = await production_client.get(
        "/v1/auth/me", headers=bearer(token),
    )
    assert rejected.status_code == 401


def test_redteam_strict_jws_header_applies_to_both_token_types():
    from backend.auth import supported_jws_header

    assert supported_jws_header({"alg": "RS256", "kid": "kid-01"})
    assert not supported_jws_header({
        "alg": "RS256", "kid": "kid-01", "crit": ["custom"],
    })
    assert not supported_jws_header({
        "alg": "RS256", "kid": "kid-01", "b64": False,
    })


@pytest.mark.asyncio
async def test_redteam_privacy_export_requires_operator_and_signed_tenant(
    signer, production_client,
):
    viewer = bearer(signer(roles=("viewer",)))
    reviewer = bearer(signer(roles=("reviewer",)))
    operator = bearer(signer(roles=("operator",)))
    for non_operator in (viewer, reviewer):
        response = await production_client.get(
            "/v1/privacy/export", headers=non_operator,
        )
        assert response.status_code == 403
    allowed = await production_client.get(
        "/v1/privacy/export", headers=operator,
    )
    assert allowed.status_code == 200
    assert allowed.json()["kind"] == "consents"
    assert allowed.json()["metadata_only"] is True
    assert (await production_client.get(
        "/v1/privacy/export?kind=capsules&limit=51", headers=operator,
    )).status_code == 422
    assert (await production_client.get(
        "/v1/privacy/export", headers=bearer(
            signer(tenant="beta", roles=("operator",)),
        ),
    )).status_code == 403


@pytest.mark.asyncio
async def test_redteam_quarantine_requires_operator_not_viewer_or_reviewer(
    signer, production_client,
):
    route = f"/v1/experiences/{uuid4()}/quarantine"
    for role in ("viewer", "reviewer"):
        denied = await production_client.post(
            route,
            headers={**bearer(signer(roles=(role,))), "If-Match": "1"},
        )
        assert denied.status_code == 403
    operator = bearer(signer(roles=("operator",)))
    assert (await production_client.post(
        route, headers={**operator, "If-Match": "broken"},
    )).status_code == 400
    assert (await production_client.post(
        route, headers={**operator, "If-Match": "1"},
    )).status_code == 404


@pytest.mark.asyncio
async def test_redteam_tenant_consent_requires_independent_security_admin(
    signer, production_client,
):
    route = "/v1/privacy/experience-consent"
    for roles in (
        ("viewer",), ("operator",), ("reviewer",),
        ("security_admin", "operator"),
    ):
        header = {"Authorization": "Bearer " + signer(roles=roles)}
        response = await production_client.post(
            route, json={"allow": False},
            headers={**header, "If-Match": "0"},
        )
        assert response.status_code == 403
    admin = {"Authorization": "Bearer " + signer(
        subject="privacy-admin", roles=("security_admin",),
    )}
    first_read = await production_client.get(route, headers=admin)
    assert first_read.status_code == 200
    assert first_read.json() == {
        "epoch": 0, "allowed": None, "policy": "PER_INCIDENT_LEGACY",
    }
    first = await production_client.post(
        route, json={"allow": False}, headers={**admin, "If-Match": "0"},
    )
    assert first.status_code == 204 and first.content == b""
    retry = await production_client.post(
        route, json={"allow": True}, headers={**admin, "If-Match": "0"},
    )
    assert retry.status_code == 409
    current = await production_client.get(route, headers=admin)
    assert current.json() == {
        "epoch": 1, "allowed": False, "policy": "TENANT_OVERRIDE",
    }
    other = {"Authorization": "Bearer " + signer(
        subject="privacy-other", tenant="beta", roles=("security_admin",),
    )}
    assert (await production_client.get(route, headers=other)).json()["epoch"] == 0
    invalid = await production_client.post(
        route, json={"allow": True}, headers={**admin, "If-Match": "bad"},
    )
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_redteam_projection_dead_letters_require_tenant_and_operator_for_review(
    signer, production_client, sessions_fixture,
):
    from uuid import UUID

    from backend.models import OutboxEntry
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "deadletter-oidc",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            item = OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": "private malformed source"},
                attempt=5, dead_letter_code="RETRY_EXHAUSTED",
                dead_lettered_at=datetime.now(timezone.utc),
            )
            session.add(item)
            await session.flush()
            dead_id = item.id

    viewer = bearer(signer(roles=("viewer",)))
    operator = bearer(signer(roles=("operator",)))
    reviewer = bearer(signer(roles=("reviewer",)))
    wrong_tenant = bearer(
        signer(tenant="beta", roles=("operator",)), tenant="beta",
    )
    list_url = "/v1/operations/dead-letters"
    own = await production_client.get(list_url, headers=viewer)
    assert own.status_code == 200
    assert [x["id"] for x in own.json()["records"]] == [dead_id]
    assert "private malformed source" not in own.text
    assert (await production_client.get(
        list_url, headers=wrong_tenant,
    )).json()["records"] == []

    review_url = f"{list_url}/{dead_id}/acknowledge"
    for role_header in (viewer, reviewer):
        assert (await production_client.post(
            review_url, headers={**role_header, "If-Match": "5"},
        )).status_code == 403
    assert (await production_client.post(
        review_url, headers={**wrong_tenant, "If-Match": "5"},
    )).status_code == 404
    assert (await production_client.post(
        review_url, headers={**operator, "If-Match": "4"},
    )).status_code == 409
    for invalid in ("broken", "0", "12345678901"):
        expected = 400 if invalid != "0" else 409
        assert (await production_client.post(
            review_url, headers={**operator, "If-Match": invalid},
        )).status_code == expected
    acknowledge = await production_client.post(
        review_url, headers={**operator, "If-Match": "5"},
    )
    assert acknowledge.status_code == 202
    assert acknowledge.json() == {
        "id": dead_id, "attempts": 5, "reviewed": True,
    }
    assert (await production_client.post(
        review_url, headers={**operator, "If-Match": "5"},
    )).status_code == 409
    refreshed = await production_client.get(list_url, headers=operator)
    assert refreshed.json()["records"][0]["reviewed"] is True

    async with sessions_fixture() as session:
        stored = await session.get(OutboxEntry, dead_id)
        assert stored.delivered_at is None
        assert stored.dead_lettered_at is not None
        assert stored.dead_letter_reviewed_by is not None
        assert stored.dead_letter_reviewed_by.startswith("oidc:")
        assert stored.incident_id == UUID(str(incident.id))


@pytest.mark.asyncio
async def test_redteam_redrive_requires_prior_operator_review_and_independent_admin(
    signer, production_client, sessions_fixture,
):
    from backend.experiences import set_consent
    from backend.models import OutboxEntry, OutcomeRevision
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "redrive-oidc",
            IncidentCreate(target_id="synthetic-only"),
        )
    async with sessions_fixture() as session:
        await set_consent(session, "alpha", incident.id, 0, True)
    outcome_id = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=outcome_id, tenant_id="alpha", incident_id=incident.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="synthetic independent verification",
            ))
            source = OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": str(outcome_id)}, attempt=5,
                created_at=datetime.now(timezone.utc),
                dead_lettered_at=datetime.now(timezone.utc),
                dead_letter_code="RETRY_EXHAUSTED",
            )
            session.add(source)
            await session.flush()
            entry_id = source.id

    operator = bearer(signer(
        subject="operator-review", roles=("operator",),
    ))
    administrator = bearer(signer(
        subject="independent-admin", roles=("security_admin",),
    ))
    viewer = bearer(signer(roles=("viewer",)))
    reviewer = bearer(signer(roles=("reviewer",)))
    combined = bearer(signer(roles=("operator", "security_admin")))
    foreign = bearer(signer(
        tenant="beta", roles=("security_admin",),
    ), tenant="beta")
    base = f"/v1/operations/dead-letters/{entry_id}"
    action = {"reason": "STORE_RESTORED"}
    headers = {"If-Match": "5"}

    for denied in (operator, viewer, reviewer, combined):
        response = await production_client.post(
            base + "/redrive", json=action, headers={**denied, **headers},
        )
        assert response.status_code == 403
    assert (await production_client.post(
        base + "/redrive", json=action,
        headers={**foreign, **headers},
    )).status_code == 404
    assert (await production_client.post(
        base + "/redrive", json=action,
        headers={**administrator, **headers},
    )).status_code == 409  # No operator review yet.
    for invalid in ({}, {"reason": "UNSAFE"}, {"reason": "STORE_RESTORED", "debug": True}):
        assert (await production_client.post(
            base + "/redrive", json=invalid,
            headers={**administrator, **headers},
        )).status_code == 422

    assert (await production_client.post(
        base + "/acknowledge", headers={**operator, **headers},
    )).status_code == 202
    assert (await production_client.post(
        base + "/redrive", json=action, headers={
            **administrator, "If-Match": "4",
        },
    )).status_code == 409
    successful = await production_client.post(
        base + "/redrive", json=action,
        headers={**administrator, **headers},
    )
    assert successful.status_code == 202, successful.text
    child_id = successful.json()["id"]
    assert successful.json() == {
        "id": child_id, "source_id": entry_id,
        "state": "PENDING_PROJECTION",
    }
    assert (await production_client.post(
        base + "/redrive", json=action,
        headers={**administrator, **headers},
    )).status_code == 409

    page = await production_client.get(
        "/v1/operations/dead-letters", headers=viewer,
    )
    assert page.json()["records"][0]["redriven_to"] == child_id
    async with sessions_fixture() as session:
        source = await session.get(OutboxEntry, entry_id)
        child = await session.get(OutboxEntry, child_id)
        assert source.dead_lettered_at is not None
        assert source.delivered_at is None
        assert child.redrive_source_id == entry_id
        assert child.redrive_authorized_by == verify_bearer(
            signer(subject="independent-admin", roles=("security_admin",)),
            AUTH_SETTINGS,
        ).subject
        assert child.redrive_authorized_by != source.dead_letter_reviewed_by
        assert child.redrive_reason == "STORE_RESTORED"
        assert child.payload == {"outcome_id": str(outcome_id)}


@pytest.mark.parametrize(("issuer", "jwks"), [
    ("https://idp.example.test/", "https://idp.example.test:9443/jwks"),
    ("https://idp.example.test:9443/", "https://idp.example.test/jwks"),
    ("https://idp.example.test/?tenant=other", "https://idp.example.test/jwks"),
    ("https://idp.example.test/#fragment", "https://idp.example.test/jwks"),
    ("https://idp.example.test/", "https://idp.example.test/jwks?kid=other"),
    ("https://idp.example.test/", "https://idp.example.test/jwks#fragment"),
    ("https://user@idp.example.test/", "https://idp.example.test/jwks"),
    ("https://idp.example.test/tenant/../other", "https://idp.example.test/jwks"),
    ("https://idp.example.test/tenant", "https://idp.example.test/%2Fkeys"),
    ("https://idp.example.test/tenant", r"https://idp.example.test/\keys"),
    ("https://idp.example.test/", "https://idp.example.test:bad/jwks"),
    ("https://idp.example.test/", "https://other.example.test/jwks"),
    ("http://idp.example.test/", "https://idp.example.test/jwks"),
])
def test_redteam_ambiguous_cross_port_or_cross_origin_jwks_is_not_configured(
    issuer, jwks,
):
    settings = Settings(
        environment="production",
        oidc_issuer=issuer, oidc_audience="opsblaze-api",
        oidc_jwks_url=jwks,
    )
    assert configured_identity(settings) is False


def test_same_default_https_port_is_a_single_valid_origin():
    assert configured_identity(Settings(
        environment="production",
        oidc_issuer="https://idp.example.test:443/tenant",
        oidc_audience="opsblaze-api",
        oidc_jwks_url="https://idp.example.test/tenant/keys",
    ))


@pytest.mark.asyncio
async def test_redteam_ambiguous_jwks_config_fails_before_fetching_token_keys(
    signer, production_client, monkeypatch,
):
    called = False

    def should_not_fetch(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid identity config attempted external key fetch")

    monkeypatch.setattr("backend.auth.signing_key", should_not_fetch)
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="production",
        oidc_issuer=ISSUER, oidc_audience=AUDIENCE,
        oidc_jwks_url="https://idp.example.test:9443/jwks",
    )
    response = await production_client.get(
        "/v1/auth/me",
        headers=bearer(signer()),
    )
    assert response.status_code == 503
    assert called is False
