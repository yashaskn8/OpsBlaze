"""Stage 11.2 Gate A: Security, STRIDE adversarial, and tenant membership tests for identity exchange."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings, get_settings
from backend.domain.auth import (
    ActorKind,
    PrincipalRole,
    mint_principal_token,
)
from backend.domain.identity_provider import (
    SSRFBlockedError,
    validate_introspection_url,
)
from backend.persistence.models import IdentityMembership, Tenant


@pytest.fixture
def idp_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={
            "idp_introspection_url": "https://idp.opsblaze.corp/oauth/introspect",
            "idp_client_id": "opsblaze-client-id",
            "idp_client_secret": "opsblaze-client-secret",
            "idp_expected_issuer": "https://idp.opsblaze.corp",
            "idp_expected_audience": "opsblaze-app",
            "auth_allow_password_login": True,
        }
    )


def mock_idp_http(mock_resp: httpx.Response):
    """Context manager mocking only the outbound HTTP client inside identity_provider."""
    mock_instance = AsyncMock()
    mock_instance.__aenter__.return_value = mock_instance
    mock_instance.__aexit__.return_value = None
    mock_instance.post.return_value = mock_resp
    return patch("backend.domain.identity_provider.httpx.AsyncClient", return_value=mock_instance)


@pytest.mark.asyncio
async def test_ssrf_validation_blocks_malicious_urls() -> None:
    # Cloud metadata IP
    with pytest.raises(SSRFBlockedError):
        validate_introspection_url("http://169.254.169.254/latest/meta-data/", is_production=False)

    # Cloud metadata host
    with pytest.raises(SSRFBlockedError):
        validate_introspection_url(
            "http://metadata.google.internal/computeMetadata/v1/", is_production=False
        )

    # Loopback in production
    with pytest.raises(SSRFBlockedError):
        validate_introspection_url("https://127.0.0.1/oauth/introspect", is_production=True)

    # Non-HTTPS in production
    with pytest.raises(SSRFBlockedError):
        validate_introspection_url("http://idp.opsblaze.corp/oauth/introspect", is_production=True)


@pytest.mark.asyncio
async def test_token_exchange_success(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    async with session_factory() as session:
        membership = IdentityMembership(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            idp_issuer="https://idp.opsblaze.corp",
            idp_subject="sub:auth0|alice_123",
            actor_id="user-alice",
            actor_kind="HUMAN",
            roles=[PrincipalRole.RELEASE_OWNER.value, PrincipalRole.OPERATOR.value],
            is_active=True,
        )
        session.add(membership)
        await session.commit()

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:auth0|alice_123",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["opsblaze-app"],
            "exp": int(time.time()) + 7200,
            "client_id": "opsblaze-client-id",
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "valid-upstream-oauth-token-12345678",
                    "ttl_seconds": 3600,
                },
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert "access_token" in data
            assert data["token_type"] == "Bearer"
            assert data["expires_in"] == 3600
            assert data["principal"]["actor_id"] == "user-alice"
            assert PrincipalRole.RELEASE_OWNER.value in data["principal"]["roles"]


@pytest.mark.asyncio
async def test_token_exchange_inactive_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": False,
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "revoked-upstream-oauth-token-12345678",
                },
            )
            assert resp.status_code == 401
            assert "inactive or revoked" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_token_exchange_issuer_mismatch_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:attacker|123",
            "iss": "https://malicious-idp.com",
            "aud": ["opsblaze-app"],
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "attacker-token-12345678",
                },
            )
            assert resp.status_code == 401
            assert "issuer" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_token_exchange_audience_mismatch_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:legit|123",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["different-app"],
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "legit-token-wrong-aud-12345678",
                },
            )
            assert resp.status_code == 401
            assert "audience" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_token_exchange_expired_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:legit|123",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["opsblaze-app"],
            "exp": int(time.time()) - 60,  # Expired 60s ago
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "expired-token-12345678",
                },
            )
            assert resp.status_code == 401
            assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_token_exchange_missing_membership_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:unregistered|999",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["opsblaze-app"],
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "unregistered-user-token-12345678",
                },
            )
            assert resp.status_code == 403
            assert (
                "no active" in resp.json()["detail"].lower()
                or "membership" in resp.json()["detail"].lower()
            )


@pytest.mark.asyncio
async def test_token_exchange_deactivated_membership_rejected(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    async with session_factory() as session:
        membership = IdentityMembership(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            idp_issuer="https://idp.opsblaze.corp",
            idp_subject="sub:deactivated|456",
            actor_id="user-deactivated",
            actor_kind="HUMAN",
            roles=[PrincipalRole.OPERATOR.value],
            is_active=False,  # Explicitly deactivated
        )
        session.add(membership)
        await session.commit()

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:deactivated|456",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["opsblaze-app"],
            "exp": int(time.time()) + 3600,
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "deactivated-user-token-12345678",
                },
            )
            assert resp.status_code == 403
            assert "deactivated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cross_tenant_membership_isolation(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    # Create a secondary tenant
    other_tenant_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=other_tenant_id, name=f"Other Tenant {uuid.uuid4().hex[:8]}"))
        await session.flush()
        # Register user in other tenant
        membership = IdentityMembership(
            id=uuid.uuid4(),
            tenant_id=other_tenant_id,
            idp_issuer="https://idp.opsblaze.corp",
            idp_subject="sub:charlie|777",
            actor_id="user-charlie",
            actor_kind="HUMAN",
            roles=[PrincipalRole.INCIDENT_COMMANDER.value],
            is_active=True,
        )
        session.add(membership)
        await session.commit()

    mock_idp_response = httpx.Response(
        200,
        json={
            "active": True,
            "sub": "sub:charlie|777",
            "iss": "https://idp.opsblaze.corp",
            "aud": ["opsblaze-app"],
            "exp": int(time.time()) + 3600,
        },
    )

    with mock_idp_http(mock_idp_response):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/auth/exchange",
                json={
                    "tenant_id": str(isolated_tenant),
                    "upstream_token": "charlie-valid-token-12345678",
                },
            )
            assert resp.status_code == 403
            assert (
                "no active" in resp.json()["detail"].lower()
                or "membership" in resp.json()["detail"].lower()
            )

    # Clean up secondary tenant
    async with session_factory() as session:
        await session.execute(
            IdentityMembership.__table__.delete().where(
                IdentityMembership.tenant_id == other_tenant_id
            )
        )
        await session.execute(Tenant.__table__.delete().where(Tenant.id == other_tenant_id))
        await session.commit()


@pytest.mark.asyncio
async def test_client_cannot_assert_roles_in_exchange(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    # Client tries to inject 'roles' or 'actor_id' in exchange request payload
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/exchange",
            json={
                "tenant_id": str(isolated_tenant),
                "upstream_token": "some-token-12345678",
                "roles": ["ADMIN", "RELEASE_OWNER"],
                "actor_id": "root-injected",
            },
        )
        # Pydantic extra='forbid' must reject extra fields with 422 Unprocessable Entity
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_zero_second_membership_revocation_guarantee(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    app = create_app(idp_settings)

    membership_id = uuid.uuid4()
    async with session_factory() as session:
        membership = IdentityMembership(
            id=membership_id,
            tenant_id=isolated_tenant,
            idp_issuer="https://idp.opsblaze.corp",
            idp_subject="sub:bob_999",
            actor_id="user-bob",
            actor_kind="HUMAN",
            roles=[PrincipalRole.OPERATOR.value],
            is_active=True,
        )
        session.add(membership)
        await session.commit()

    # Mint a token for Bob
    token = mint_principal_token(
        actor_id="user-bob",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=frozenset({PrincipalRole.OPERATOR.value}),
        secret_key=idp_settings.auth_secret_key or "",
        ttl_seconds=3600,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Request 1: Valid while active
        resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["actor_id"] == "user-bob"

        # Operator deactivates Bob's membership in database
        async with session_factory() as session:
            m = await session.get(IdentityMembership, membership_id)
            assert m is not None
            m.is_active = False
            await session.commit()

        # Request 2: Immediate 0-second rejection with 401 Unauthorized
        resp2 = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp2.status_code == 401
        assert "deactivated" in resp2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_production_rejects_password_login(
    test_settings: Settings,
    isolated_tenant: uuid.UUID,
) -> None:
    prod_settings = test_settings.model_copy(update={"auth_allow_password_login": False})
    app = create_app(prod_settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={
                "username": "operator",
                "password": "dev-password-secure",
                "tenant_id": str(isolated_tenant),
            },
        )
        assert resp.status_code == 403
        assert "disabled in production" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_identity_exchange_derives_roles_from_server_membership(
    api_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://identity.example.test/"
    settings = get_settings().model_copy(
        update={
            "identity_issuer": issuer,
            "identity_introspection_url": "https://identity.example.test/introspect",
            "identity_client_id": "opsblaze-api",
            "identity_client_secret": "test-only-secret",
            "identity_audience": "opsblaze-api",
        }
    )
    monkeypatch.setattr("backend.api.auth.get_settings", lambda: settings)

    async def fake_introspection(
        token: str,
        *,
        issuer: str,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        audience: str,
    ) -> dict:
        assert token == "provider-access-token"
        assert introspection_url.endswith("/introspect")
        assert client_id == "opsblaze-api"
        assert client_secret == "test-only-secret"
        assert audience == "opsblaze-api"
        return {
            "active": True,
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + 300,
            "sub": "immutable-provider-subject-123",
        }

    monkeypatch.setattr("backend.api.auth.introspect_access_token", fake_introspection)

    async with session_factory() as session:
        session.add(
            IdentityMembership(
                id=uuid.uuid4(),
                issuer=issuer.rstrip("/"),
                subject="immutable-provider-subject-123",
                tenant_id=isolated_tenant,
                actor_id="ops-user-123",
                actor_kind="HUMAN",
                roles=["release_owner"],
                is_active=True,
            )
        )
        await session.commit()

    response = await api_client.post(
        "/v1/auth/exchange",
        json={
            "access_token": "provider-access-token",
            "tenant_id": str(isolated_tenant),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 900
    assert body["principal"]["actor_id"] == "ops-user-123"
    assert body["principal"]["roles"] == ["release_owner"]
    assert body["principal"]["tenant_id"] == str(isolated_tenant)

    me = await api_client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["actor_id"] == "ops-user-123"


@pytest.mark.asyncio
async def test_identity_exchange_rejects_subject_without_tenant_membership(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://identity.example.test/"
    settings = get_settings().model_copy(
        update={
            "identity_issuer": issuer,
            "identity_introspection_url": "https://identity.example.test/introspect",
            "identity_client_id": "opsblaze-api",
            "identity_client_secret": "test-only-secret",
            "identity_audience": "opsblaze-api",
        }
    )
    monkeypatch.setattr("backend.api.auth.get_settings", lambda: settings)

    async def fake_introspection(
        token: str,
        *,
        issuer: str,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        audience: str,
    ) -> dict:
        return {
            "active": True,
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + 300,
            "sub": "unprovisioned-subject",
        }

    monkeypatch.setattr("backend.api.auth.introspect_access_token", fake_introspection)
    response = await api_client.post(
        "/v1/auth/exchange",
        json={"access_token": "provider-access-token", "tenant_id": str(isolated_tenant)},
    )
    assert response.status_code == 403
    assert "server-managed membership" in response.json()["detail"]


@pytest.mark.asyncio
async def test_identity_exchange_fails_closed_when_provider_is_not_configured(
    api_client: AsyncClient,
) -> None:
    response = await api_client.post(
        "/v1/auth/exchange",
        json={
            "access_token": "provider-access-token",
            "tenant_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_identity_exchange_hard_deleted_membership_fails_closed(
    idp_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Verifies that if an enterprise IdP user membership is deleted (not just deactivated),

    subsequent requests with an oauth2_token_introspection token fail closed with HTTP 401.
    """
    app = create_app(idp_settings)

    membership_id = uuid.uuid4()
    async with session_factory() as session:
        membership = IdentityMembership(
            id=membership_id,
            tenant_id=isolated_tenant,
            idp_issuer="https://idp.opsblaze.corp",
            idp_subject="sub:charlie_deleted",
            actor_id="user-charlie",
            actor_kind="HUMAN",
            roles=[PrincipalRole.OPERATOR.value],
            is_active=True,
        )
        session.add(membership)
        await session.commit()

    token = mint_principal_token(
        actor_id="user-charlie",
        actor_kind=ActorKind.HUMAN,
        tenant_id=isolated_tenant,
        roles=frozenset({PrincipalRole.OPERATOR.value}),
        secret_key=idp_settings.auth_secret_key or "",
        ttl_seconds=3600,
        auth_mechanism="oauth2_token_introspection",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Request 1: Valid while membership exists
        resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["actor_id"] == "user-charlie"

        # Hard delete Charlie's membership row
        async with session_factory() as session:
            m = await session.get(IdentityMembership, membership_id)
            assert m is not None
            await session.delete(m)
            await session.commit()

        # Request 2: Must fail closed with 401 Unauthorized immediately
        resp2 = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp2.status_code == 401
        assert "membership does not exist or is deactivated" in resp2.json()["detail"].lower()
