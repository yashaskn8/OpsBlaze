"""HTTP-level contract checks for tenant isolation and fail-closed controls."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api import app
from backend.database import get_session
from backend.settings import Settings, get_settings

TOKEN = "test-local-token"


@pytest.fixture
async def api_client(
    sessions_fixture: async_sessionmaker,
) -> AsyncIterator[AsyncClient]:
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
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def headers(tenant: str, **extra: str) -> dict[str, str]:
    return {"X-Tenant-Id": tenant, "X-OpsBlaze-Token": TOKEN, **extra}


@pytest.mark.asyncio
async def test_isolation_across_snapshot_lists_events_and_lineage(api_client):
    response = await api_client.post(
        "/v1/incidents",
        json={"target_id": "checkout"},
        headers=headers("alpha", **{"Idempotency-Key": "key-1"}),
    )
    assert response.status_code == 202, response.text
    incident_id = response.json()["id"]
    path = f"/v1/incidents/{incident_id}"

    own = await api_client.get(path, headers=headers("alpha"))
    assert own.status_code == 200 and own.headers["etag"] == '"1"'
    listing = await api_client.get("/v1/incidents", headers=headers("alpha"))
    assert [row["id"] for row in listing.json()] == [incident_id]

    other_listing = await api_client.get("/v1/incidents", headers=headers("beta"))
    assert other_listing.status_code == 200 and other_listing.json() == []

    for suffix in ("", "/evidence", "/hypotheses", "/effects", "/outcomes",
                   "/plans", "/assessments", "/events", "/stream", "/lineage", "/route"):
        hidden = await api_client.get(path + suffix, headers=headers("beta"))
        assert hidden.status_code == 404, (suffix, hidden.text)

    own_events = await api_client.get(path + "/events", headers=headers("alpha"))
    assert own_events.status_code == 200
    events = own_events.json()
    assert len(events["events"]) == 1
    assert events["events"][0]["kind"] == "ADMITTED"
    later = await api_client.get(
        path + "/events",
        params={"after_id": events["last_id"]},
        headers=headers("alpha"),
    )
    assert later.status_code == 200 and later.json()["events"] == []


@pytest.mark.asyncio
async def test_version_bound_cancel_and_changed_idempotency_payload(api_client):
    request_headers = headers("alpha", **{"Idempotency-Key": "stable"})
    created = await api_client.post(
        "/v1/incidents", json={"target_id": "checkout"},
        headers=request_headers,
    )
    assert created.status_code == 202
    incident_id = created.json()["id"]
    replay = await api_client.post(
        "/v1/incidents", json={"target_id": "checkout"},
        headers=request_headers,
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == incident_id
    conflict = await api_client.post(
        "/v1/incidents", json={"target_id": "payments"},
        headers=request_headers,
    )
    assert conflict.status_code == 409

    path = f"/v1/incidents/{incident_id}"
    stale = await api_client.post(path + "/cancel", headers=headers("alpha", **{"If-Match": "0"}))
    assert stale.status_code == 409
    wrong_tenant = await api_client.post(path + "/cancel", headers=headers("beta", **{"If-Match": "1"}))
    assert wrong_tenant.status_code == 404
    cancelled = await api_client.post(path + "/cancel", headers=headers("alpha", **{"If-Match": '"1"'}))
    assert cancelled.status_code == 202 and cancelled.json()["status_version"] == 2
    repeated = await api_client.post(path + "/cancel", headers=headers("alpha", **{"If-Match": "2"}))
    assert repeated.status_code == 409
    events = await api_client.get(path + "/events", headers=headers("alpha"))
    assert [event["kind"] for event in events.json()["events"]] == [
        "ADMITTED", "CANCEL_REQUESTED",
    ]


@pytest.mark.asyncio
async def test_production_mode_and_unvalidated_release_fail_closed(api_client):
    denied = await api_client.get(
        "/v1/incidents", headers=headers("alpha", **{"X-OpsBlaze-Token": "wrong"})
    )
    assert denied.status_code == 401
    missing = await api_client.get("/v1/incidents", headers={"X-Tenant-Id": "alpha"})
    assert missing.status_code == 422
    bad_tenant = await api_client.get("/v1/incidents", headers=headers("bad/tenant"))
    assert bad_tenant.status_code == 422

    # No environment or developer header can approve a live policy rollout.
    release = await api_client.post(
        f"/v1/rollouts/{uuid4()}/approve",
        headers=headers("alpha"),
    )
    assert release.status_code == 409
    assert "release gate closed" in release.json()["detail"]

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        local_token=TOKEN,
        environment="production",
    )
    production = await api_client.get("/v1/incidents", headers=headers("alpha"))
    assert production.status_code == 503


@pytest.mark.asyncio
async def test_stream_rejects_invalid_resume_cursor_before_open(api_client):
    created = await api_client.post(
        "/v1/incidents",
        json={"target_id": "checkout"},
        headers=headers("alpha", **{"Idempotency-Key": "cursor"}),
    )
    incident_id = created.json()["id"]
    path = f"/v1/incidents/{incident_id}/stream"
    negative = await api_client.get(path + "?after_id=-1", headers=headers("alpha"))
    assert negative.status_code == 400
    malformed = await api_client.get(
        path, headers=headers("alpha", **{"Last-Event-ID": "nope"})
    )
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_plan_decision_requires_distinct_reviewer_token(api_client):
    plan_id = uuid4()
    path = f"/v1/plans/{plan_id}/decisions"
    body = {"decision": "APPROVE"}
    base = headers("alpha", **{"If-Match": "1"})

    not_configured = await api_client.post(path, json=body, headers=base)
    assert not_configured.status_code == 503

    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token=TOKEN, review_token="separate-reviewer-credential",
        environment="development",
    )
    missing = await api_client.post(path, json=body, headers=base)
    assert missing.status_code == 403
    wrong = await api_client.post(
        path, json=body,
        headers={**base, "X-OpsBlaze-Reviewer-Token": TOKEN},
    )
    assert wrong.status_code == 403
    authorized = await api_client.post(
        path, json=body,
        headers={**base, "X-OpsBlaze-Reviewer-Token": "separate-reviewer-credential"},
    )
    assert authorized.status_code == 404

    # The same token cannot be both operator and reviewer.
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token=TOKEN, review_token=TOKEN, environment="development",
    )
    same = await api_client.post(
        path, json=body,
        headers={**base, "X-OpsBlaze-Reviewer-Token": TOKEN},
    )
    assert same.status_code == 503


@pytest.mark.asyncio
async def test_review_actions_require_separate_local_reviewer(api_client):
    reviewer_routes = [
        ("post", f"/v1/outcomes/{uuid4()}/feedback",
         {"reason": "independent observation changed"}),
        ("post", f"/v1/failures/{uuid4()}/review",
         {"corrected_code": "PROBE_DENIED", "first_bad_step": 1,
          "reason": "durable probe evidence was checked"}),
        ("post", f"/v1/improvements/{uuid4()}/submit", None),
        ("post", f"/v1/lessons/{uuid4()}/validate", None),
        ("post", f"/v1/lessons/{uuid4()}/revoke", None),
        ("post", f"/v1/rollouts/{uuid4()}/pause", None),
    ]
    base = headers("alpha", **{"If-Match": "1"})
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token=TOKEN, review_token="separate-reviewer-credential",
        environment="development",
    )
    for method, path, body in reviewer_routes:
        denied = await api_client.request(method, path, headers=base, json=body)
        assert denied.status_code == 403, (path, denied.text)
        granted = await api_client.request(
            method, path,
            headers={**base, "X-OpsBlaze-Reviewer-Token": "separate-reviewer-credential"},
            json=body,
        )
        assert granted.status_code == 404, (path, granted.text)


@pytest.mark.asyncio
async def test_sse_overload_returns_retryable_status(api_client, monkeypatch):
    from backend.events import StreamCapacity

    created = await api_client.post(
        "/v1/incidents", json={"target_id": "checkout"},
        headers=headers("alpha", **{"Idempotency-Key": "capacity-sse"}),
    )
    assert created.status_code == 202
    limit = StreamCapacity(maximum=1)
    assert limit.claim()
    monkeypatch.setattr("backend.events.STREAM_CAPACITY", limit)
    result = await api_client.get(
        f"/v1/incidents/{created.json()['id']}/stream",
        headers=headers("alpha"),
    )
    assert result.status_code == 503
    assert result.headers["retry-after"] == "3"
    limit.release()


@pytest.mark.asyncio
async def test_approval_revocation_requires_reviewer_and_version(api_client):
    path = f"/v1/approvals/{uuid4()}/revoke"
    body = {"reason": "reviewer revoked the proposed simulator action"}
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token=TOKEN, review_token="separate-reviewer-credential",
        environment="development",
    )
    base = headers("alpha", **{"If-Match": "1"})
    denied = await api_client.post(path, headers=base, json=body)
    assert denied.status_code == 403
    approved = await api_client.post(
        path,
        headers={
            **base,
            "X-OpsBlaze-Reviewer-Token": "separate-reviewer-credential",
        },
        json=body,
    )
    assert approved.status_code == 404
    malformed_version = await api_client.post(
        path,
        headers={
            **base,
            "If-Match": "bogus",
            "X-OpsBlaze-Reviewer-Token": "separate-reviewer-credential",
        },
        json=body,
    )
    assert malformed_version.status_code == 400


@pytest.mark.asyncio
async def test_expired_notification_cursor_requires_snapshot_resync(api_client, sessions_fixture):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from backend.models import UIEvent
    from backend.workers.event_retention import purge_ui_events

    created = await api_client.post(
        "/v1/incidents",
        headers=headers("alpha", **{"Idempotency-Key": "event-gap"}),
        json={"target_id": "checkout"},
    )
    assert created.status_code == 202
    incident_id = created.json()["id"]
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(UIEvent).where(UIEvent.incident_id == uuid4())
                .values(kind="NO_MATCH")
            )
            await session.execute(
                update(UIEvent)
                .where(UIEvent.tenant_id == "alpha")
                .values(created_at=datetime.now(timezone.utc) - timedelta(days=10))
            )
    assert await purge_ui_events(
        sessions_fixture, datetime.now(timezone.utc) - timedelta(days=7),
    ) == 1
    route = f"/v1/incidents/{incident_id}"
    stale = await api_client.get(route + "/events?after_id=0", headers=headers("alpha"))
    assert stale.status_code == 410
    watermark = stale.json()["detail"]["min_after_id"]
    stream = await api_client.get(route + "/stream?after_id=0", headers=headers("alpha"))
    assert stream.status_code == 410
    resumed = await api_client.get(
        route + f"/events?after_id={watermark}", headers=headers("alpha"),
    )
    assert resumed.status_code == 200
    assert resumed.json()["events"] == []
    snapshot = await api_client.get(route, headers=headers("alpha"))
    assert snapshot.status_code == 200


@pytest.mark.asyncio
async def test_development_identity_never_grants_reviewer_role(api_client):
    response = await api_client.get(
        "/v1/auth/me", headers=headers("alpha"),
    )
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "alpha",
        "actor_id": "DEV_OPERATOR_CREDENTIAL",
        "roles": ["operator", "viewer"],
    }


@pytest.mark.asyncio
async def test_authorized_reconcile_queries_existing_effect_without_reapply(
    api_client, sessions_fixture,
):
    from sqlalchemy import select

    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects.gateway import execute_simulator_effect
    from backend.effects.reservations import reserve_simulator_target
    from backend.models import (
        EffectReceipt, ResourceEpoch, SimulatorAppliedEffect, SimulatorTarget,
    )
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="alpha", target_id="checkout",
                version=1, healthy=False, last_fence=0,
            ))
            session.add(ResourceEpoch(
                tenant_id="alpha", target_id="checkout",
                latest_fence=0, version_seen=1,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "effect-reconcile",
            IncidentCreate(target_id="checkout"),
        )
    permit = await reserve_simulator_target(
        sessions_fixture, "alpha", "checkout", incident.id, "7" * 64,
    )

    class LostAckAdapter(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            await super().apply(effect_id, permit)
            raise TimeoutError("simulator committed but acknowledgment was lost")

    assert await execute_simulator_effect(
        sessions_fixture, LostAckAdapter(sessions_fixture), permit,
    ) == "UNKNOWN"
    async with sessions_fixture() as session:
        receipt = await session.scalar(
            select(EffectReceipt).where(EffectReceipt.incident_id == incident.id)
        )
        effect_id = receipt.id
        assert receipt.state == "UNKNOWN" and receipt.attempt_count == 1

    path = f"/v1/effects/{effect_id}/reconcile"
    hidden = await api_client.post(path, headers=headers("beta"))
    assert hidden.status_code == 404
    denied = await api_client.post(path, headers={
        "X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "invalid",
    })
    assert denied.status_code == 401
    reconciled = await api_client.post(path, headers=headers("alpha"))
    assert reconciled.status_code == 202, reconciled.text
    assert reconciled.json() == {
        "effect_id": str(effect_id), "state": "VERIFIED",
    }
    repeated = await api_client.post(path, headers=headers("alpha"))
    assert repeated.status_code == 202
    assert repeated.json()["state"] == "VERIFIED"
    async with sessions_fixture() as session:
        applied = (await session.scalars(select(SimulatorAppliedEffect))).all()
        stored = await session.get(EffectReceipt, effect_id)
        assert len(applied) == 1
        assert stored.state == "VERIFIED"
        assert stored.attempt_count == 1


@pytest.mark.asyncio
async def test_reconcile_prepared_receipt_never_infers_a_dispatch(
    api_client, sessions_fixture,
):
    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects.gateway import prepare_receipt
    from backend.effects.reservations import reserve_simulator_target
    from backend.models import ResourceEpoch, SimulatorTarget
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="alpha", target_id="checkout",
                version=1, healthy=False, last_fence=0,
            ))
            session.add(ResourceEpoch(
                tenant_id="alpha", target_id="checkout",
                latest_fence=0, version_seen=1,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "alpha", "unsent-receipt",
            IncidentCreate(target_id="checkout"),
        )
    permit = await reserve_simulator_target(
        sessions_fixture, "alpha", "checkout", incident.id, "8" * 64,
    )
    effect_id = await prepare_receipt(sessions_fixture, permit)
    response = await api_client.post(
        f"/v1/effects/{effect_id}/reconcile", headers=headers("alpha"),
    )
    assert response.status_code == 409
    assert await SimulatorAdapter(sessions_fixture).observe(
        "alpha", "checkout",
    ) == (1, False)


@pytest.mark.asyncio
async def test_admission_uses_request_settings_for_both_capacity_limits(
    api_client, monkeypatch,
):
    # An injected app configuration must not silently use process defaults.
    def unexpected_settings():
        raise AssertionError("admission reread global settings")

    monkeypatch.setattr("backend.services.get_settings", unexpected_settings)
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="development", local_token=TOKEN,
        max_active_incidents_per_tenant=1,
        max_active_incidents_global=10,
        admission_limit_per_minute=1,
    )

    async def submit(tenant, key):
        return await api_client.post(
            "/v1/incidents", json={"target_id": "simulator-only"},
            headers=headers(tenant, **{"Idempotency-Key": key}),
        )

    first = await submit("alpha", "first")
    assert first.status_code == 202
    replay = await submit("alpha", "first")
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    limited = await submit("alpha", "second")
    assert limited.status_code == 429
    assert "tenant active" in limited.json()["detail"]
    assert (await submit("beta", "first")).status_code == 202

    # Raise the active cap and use a fixed minute for the rate-limit case.
    from datetime import datetime, timezone

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("backend.services.datetime", FixedClock)
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="development", local_token=TOKEN,
        max_active_incidents_per_tenant=5,
        max_active_incidents_global=10,
        admission_limit_per_minute=1,
    )
    assert (await submit("gamma", "first")).status_code == 202
    rate_limited = await submit("gamma", "second")
    assert rate_limited.status_code == 429
    assert "tenant admission limit" in rate_limited.json()["detail"]
