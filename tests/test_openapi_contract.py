"""OpenAPI schema, fail-closed auth, and HTTP security header contract tests."""

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
import pytest

from backend.api import app
from backend.database import get_session
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings

TOKEN = "contract-op-token"


@pytest.fixture
async def contract_client(sessions_fixture) -> AsyncIterator[AsyncClient]:
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        local_token=TOKEN,
        environment="development",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://contracttest",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_openapi_schema_is_valid_and_covers_v1_routes():
    spec = app.openapi()
    assert spec["openapi"].startswith("3.")
    paths = spec["paths"]
    v1_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/")
    }
    assert v1_paths <= set(paths.keys())
    assert len(spec["components"]["schemas"]) >= 3


@pytest.mark.asyncio
async def test_unauthenticated_v1_endpoints_fail_closed(contract_client: AsyncClient):
    sample_id = str(uuid4())
    for method, path in (
        ("GET", "/v1/auth/me"),
        ("GET", "/v1/incidents"),
        ("GET", f"/v1/incidents/{sample_id}"),
        ("GET", f"/v1/incidents/{sample_id}/evidence"),
        ("GET", f"/v1/incidents/{sample_id}/evidence/lifecycle"),
        ("POST", "/v1/incidents"),
    ):
        res = await contract_client.request(method, path)
        assert res.status_code in {401, 403, 422}, (method, path, res.status_code)


@pytest.mark.asyncio
async def test_http_security_headers_and_sse_cache_control(
    contract_client: AsyncClient, sessions_fixture,
):
    async with sessions_fixture() as session:
        inc = await admit_incident(
            session, "sec-t", "sec-key-1", IncidentCreate(target_id="checkout"),
        )
    hdrs = {"X-Tenant-Id": "sec-t", "X-OpsBlaze-Token": TOKEN}
    for path in (
        "/v1/health/ready",
        "/v1/auth/me",
        "/v1/incidents",
        f"/v1/incidents/{inc.id}/evidence",
    ):
        res = await contract_client.get(path, headers=hdrs)
        assert res.headers.get("X-Content-Type-Options") == "nosniff", path
        assert res.headers.get("X-Frame-Options") == "DENY", path
        assert res.headers.get("Referrer-Policy") == "no-referrer", path
        assert res.headers.get("Cache-Control") == "no-store", path
        directives = set(res.headers["content-security-policy"].split("; "))
        assert {"default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"} <= directives
        assert "strict-transport-security" not in res.headers


@pytest.mark.asyncio
async def test_docs_keep_interactive_assets_and_hash_inline_scripts():
    import base64
    import hashlib
    import re

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["strict-transport-security"] == "max-age=31536000"
            policy = response.headers["content-security-policy"]
            assert ("worker-src blob:" in policy) is (path == "/redoc")
            assert ("https://cdn.redoc.ly/redoc/logo-mini.svg" in policy) is (path == "/redoc")
            scripts = re.findall(rb"<script>(.*?)</script>", response.content, flags=re.S)
            for script in scripts:
                digest = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
                assert f"'sha256-{digest}'" in policy
            script_policy = next(
                (directive for directive in policy.split("; ") if directive.startswith("script-src ")),
                "",
            )
            assert "'unsafe-inline'" not in script_policy
        assert "/openapi.json" in (await client.get("/docs")).text


@pytest.mark.asyncio
async def test_security_headers_cover_error_responses(monkeypatch):
    monkeypatch.setattr(app.router, "routes", app.router.routes.copy())

    @app.get("/v1/security-header-fixture", include_in_schema=False)
    async def fail():
        raise RuntimeError("disposable error response fixture")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path, status in (("/v1/nonexistent", 404), ("/v1/security-header-fixture", 500)):
            response = await client.get(path)
            assert response.status_code == status
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store"


def test_browser_policy_allows_only_the_configured_https_issuer_origin():
    from backend.docs_ui import CONTENT_POLICY, browser_content_policy

    configured = browser_content_policy("https://idp.example.test:8443/realm")
    assert "connect-src 'self' https://idp.example.test:8443" in configured
    assert "/realm" not in configured
    assert browser_content_policy("http://idp.example.test") == CONTENT_POLICY
