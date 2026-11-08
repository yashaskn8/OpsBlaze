"""Security and adversarial tests for Stage 11.1 Gate A — Authentication and Secret Management."""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from backend.api.main import create_app
from backend.config import Settings
from backend.domain.auth import (
    ActorKind,
    AuthenticationError,
    AuthorizationForbiddenError,
    Principal,
    TokenRevocationStore,
    mint_principal_token,
    verify_principal_token,
)


@pytest.mark.asyncio
async def test_missing_production_secrets_prevent_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing auth or policy signing secrets in production environment must fail startup."""
    monkeypatch.setenv("OPSBLAZE_ENV", "production")
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("POLICY_SIGNING_SECRET", raising=False)

    with pytest.raises((ValidationError, ValueError)) as exc_info:
        Settings(
            opsblaze_env="production",
            auth_secret_key=None,  # type: ignore[arg-type]
            policy_signing_secret=None,  # type: ignore[arg-type]
        )
    assert "AUTH_SECRET_KEY" in str(exc_info.value) or "policy_signing_secret" in str(exc_info.value)


@pytest.mark.asyncio
async def test_compromised_default_secret_rejected_in_production() -> None:
    """Default hardcoded strings from prior versions must be rejected in production as compromised."""
    with pytest.raises((ValidationError, ValueError)) as exc_info:
        Settings(
            opsblaze_env="production",
            auth_secret_key="opsblaze-secret-auth-signing-key-2026-production",
            policy_signing_secret="opsblaze-immutable-bundle-hmac-sha256-signer-key-2026",
        )
    assert "compromised" in str(exc_info.value).lower() or "insecure" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_token_algorithm_strictness() -> None:
    """Tokens with 'none' or asymmetric algorithms must be rejected."""
    secret = "a" * 32
    tenant_id = uuid.uuid4()

    # Forge header with alg="none"
    hdr_b64 = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    payload = {
        "sub": "attacker",
        "kind": "HUMAN",
        "tenant_id": str(tenant_id),
        "roles": ["release_owner", "incident_commander"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": str(uuid.uuid4()),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    forged_token = f"{hdr_b64}.{payload_b64}."

    with pytest.raises(AuthenticationError) as exc_info:
        verify_principal_token(forged_token, secret)
    assert "algorithm" in str(exc_info.value).lower() or "signature" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_token_revocation() -> None:
    """Revoked tokens must be rejected even if unexpired and cryptographically valid."""
    secret = "a" * 32
    tenant_id = uuid.uuid4()
    jti = str(uuid.uuid4())

    token = mint_principal_token(
        actor_id="user-1",
        actor_kind=ActorKind.HUMAN,
        tenant_id=tenant_id,
        roles=["operator"],
        secret_key=secret,
        jti=jti,
    )

    # Valid before revocation
    principal = verify_principal_token(token, secret)
    assert principal.actor_id == "user-1"

    # Revoke
    TokenRevocationStore.revoke(jti)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            verify_principal_token(token, secret)
        assert "revoked" in str(exc_info.value).lower()
    finally:
        TokenRevocationStore.unrevoke(jti)


@pytest.mark.asyncio
async def test_model_cannot_claim_human_authority() -> None:
    """A bot or model actor kind cannot execute human approvals or evaluations."""
    principal = Principal(
        actor_id="model-gpt4",
        actor_kind=ActorKind.MODEL,
        tenant_id=uuid.uuid4(),
        roles=frozenset({"release_owner", "incident_commander"}),
    )

    with pytest.raises(AuthorizationForbiddenError) as exc_info:
        principal.require_human()
    assert "Only verified human operators" in str(exc_info.value)

    with pytest.raises(AuthorizationForbiddenError):
        principal.require_release_approver()

    with pytest.raises(AuthorizationForbiddenError):
        principal.require_evaluation_reviewer()


@pytest.mark.asyncio
async def test_public_token_endpoint_rejects_arbitrary_client_roles(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """The /v1/auth/token endpoint must not allow callers to mint arbitrary self-selected roles."""
    res = await api_client.post(
        "/v1/auth/token",
        json={
            "actor_id": "mallory",
            "roles": ["release_owner", "incident_commander"],
            "tenant_id": str(isolated_tenant),
        },
    )
    # Should be rejected with 401 Unauthorized
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_user_login_uses_server_derived_roles(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """Logging in via trusted server-side registry derives roles strictly on the server."""
    res = await api_client.post(
        "/v1/auth/login",
        json={
            "username": "release_owner",
            "password": "dev-password-secure",
            "tenant_id": str(isolated_tenant),
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert "release_owner" in data["principal"]["roles"]
    assert data["principal"]["actor_kind"] == "HUMAN"
    assert data["principal"]["tenant_id"] == str(isolated_tenant)


@pytest.mark.asyncio
async def test_password_registry_login_fails_closed_outside_development(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built-in development registry must never be used as production authentication."""
    production_settings = Settings(
        postgres_password="ci-test-password",
        opsblaze_env="production",
        auth_secret_key="a" * 32,
        policy_signing_secret="b" * 32,
    )
    monkeypatch.setattr("backend.api.auth.get_settings", lambda: production_settings)

    response = await api_client.post(
        "/v1/auth/login",
        json={
            "username": "release_owner",
            "password": "dev-password-secure",
            "tenant_id": str(isolated_tenant),
        },
    )
    assert response.status_code == 503
    assert "trusted identity provider" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_revocation_is_persisted_and_scoped_to_the_current_token(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """Only the current token can be revoked and PostgreSQL revocation blocks later use."""
    login = await api_client.post(
        "/v1/auth/login",
        json={
            "username": "release_owner",
            "password": "dev-password-secure",
            "tenant_id": str(isolated_tenant),
        },
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    forbidden = await api_client.post(
        "/v1/auth/revoke",
        headers=headers,
        json={"jti": "not-the-current-token"},
    )
    assert forbidden.status_code == 403

    still_valid = await api_client.get("/v1/auth/me", headers=headers)
    assert still_valid.status_code == 200

    revoked = await api_client.post("/v1/auth/revoke", headers=headers, json={})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "REVOKED"

    rejected = await api_client.get("/v1/auth/me", headers=headers)
    assert rejected.status_code == 401



@pytest.mark.asyncio
async def test_revocation_survives_new_application_instance(
    api_client: AsyncClient,
    test_settings,
    isolated_tenant: uuid.UUID,
) -> None:
    """A second independently constructed API app observes the PostgreSQL revocation record."""
    login = await api_client.post(
        "/v1/auth/login",
        json={
            "username": "release_owner",
            "password": "dev-password-secure",
            "tenant_id": str(isolated_tenant),
        },
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    revoked = await api_client.post("/v1/auth/revoke", headers=headers, json={})
    assert revoked.status_code == 200

    second_app = create_app(test_settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=second_app),
            base_url="http://second-process.test",
        ) as second_client:
            response = await second_client.get("/v1/auth/me", headers=headers)
            assert response.status_code == 401
            assert "revoked" in response.json()["detail"].lower()
    finally:
        if getattr(second_app.state, "engine", None) is not None:
            await second_app.state.engine.dispose()
