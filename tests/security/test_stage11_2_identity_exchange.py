"""Stage 11.2 — production identity exchange and tenant membership tests."""

from __future__ import annotations

import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.persistence.models import IdentityMembership


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
