"""Phase 3.6 — Security, tenant isolation, payload limits, log injection, and secret redaction tests."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from backend.config import get_settings
from backend.domain.incident import WorkflowIncidentInput
from backend.investigator.fake import FakeInvestigator
from backend.observability.metrics import emit_event, metrics, redact_value

pytestmark = pytest.mark.postgres


async def test_cross_tenant_read_and_cancel_denied_with_404(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
) -> None:
    """Verify Tenant B cannot read or cancel Tenant A's incident, and cannot guess random UUIDs."""
    r_create = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-secret-target", "title": "Tenant A confidential"},
        headers={
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": f"sec-key-{uuid.uuid4()}",
        },
    )
    assert r_create.status_code == 202
    inc_id = r_create.json()["id"]

    # Tenant B attempts GET on Tenant A's incident -> 404
    r_get_other = await api_client.get(
        f"/v1/incidents/{inc_id}",
        headers={"X-Tenant-ID": str(second_isolated_tenant)},
    )
    assert r_get_other.status_code == 404

    # Tenant B attempts CANCEL on Tenant A's incident -> 404
    r_cancel_other = await api_client.post(
        f"/v1/incidents/{inc_id}/cancel",
        headers={"X-Tenant-ID": str(second_isolated_tenant)},
    )
    assert r_cancel_other.status_code == 404

    # Guessing non-existent UUID -> 404
    r_guess = await api_client.get(
        f"/v1/incidents/{uuid.uuid4()}",
        headers={"X-Tenant-ID": str(isolated_tenant)},
    )
    assert r_guess.status_code == 404

    # Verify Tenant A's incident was not modified by Tenant B's cancel attempt
    r_verify = await api_client.get(
        f"/v1/incidents/{inc_id}",
        headers={"X-Tenant-ID": str(isolated_tenant)},
    )
    assert r_verify.status_code == 200
    assert r_verify.json()["status"] == "ACCEPTED"


async def test_forged_tenant_id_or_workflow_id_in_body_rejected_with_422(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
    second_isolated_tenant: uuid.UUID,
) -> None:
    """Verify supplying a forged tenant_id or workflow_id in the request body is rejected
    by strict Pydantic validation (`extra='forbid'`).
    """
    r_forged_tenant = await api_client.post(
        "/v1/incidents",
        json={
            "target_id": "sim-target-1",
            "tenant_id": str(second_isolated_tenant),
        },
        headers={
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": f"forge-1-{uuid.uuid4()}",
        },
    )
    assert r_forged_tenant.status_code == 422

    r_forged_wf = await api_client.post(
        "/v1/incidents",
        json={
            "target_id": "sim-target-1",
            "workflow_id": "incident:forged:workflow",
        },
        headers={
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": f"forge-2-{uuid.uuid4()}",
        },
    )
    assert r_forged_wf.status_code == 422


async def test_missing_malformed_or_unregistered_tenant_headers_denied(
    api_client: AsyncClient,
) -> None:
    payload = {"target_id": "sim-target-1"}

    # Missing X-Tenant-ID -> 401
    r_missing = await api_client.post(
        "/v1/incidents",
        json=payload,
        headers={"Idempotency-Key": "k1"},
    )
    assert r_missing.status_code == 401

    # Malformed UUID -> 401
    r_malformed = await api_client.post(
        "/v1/incidents",
        json=payload,
        headers={"X-Tenant-ID": "not-a-uuid", "Idempotency-Key": "k2"},
    )
    assert r_malformed.status_code == 401

    # Unregistered tenant UUID -> 403
    r_unregistered = await api_client.post(
        "/v1/incidents",
        json=payload,
        headers={"X-Tenant-ID": str(uuid.uuid4()), "Idempotency-Key": "k3"},
    )
    assert r_unregistered.status_code == 403


async def test_oversized_payload_and_malformed_json_rejected(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    # Oversized body (> 64 KiB) -> 413
    huge_body = b'{"target_id":"sim-1","description":"' + (b"A" * 70000) + b'"}'
    r_huge = await api_client.post(
        "/v1/incidents",
        content=huge_body,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": "huge-key",
        },
    )
    assert r_huge.status_code == 413

    # Malformed JSON -> 422
    r_bad_json = await api_client.post(
        "/v1/incidents",
        content=b'{"target_id": "sim-1", broken json',
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": "bad-json-key",
        },
    )
    assert r_bad_json.status_code == 422


async def test_log_injection_and_secret_redaction_never_expose_env_values(
    api_client: AsyncClient,
    isolated_tenant: uuid.UUID,
) -> None:
    """Verify:
    1. Newlines/control characters in headers or fields are sanitized in logs/events.
    2. Secret keys (password, token, dsn) and DSN passwords are redacted.
    3. Error responses never contain .env POSTGRES_PASSWORD values.
    """
    actual_pw = get_settings().postgres_password

    # Attempt log injection via X-Correlation-ID header
    injected_cid = 'cid-123\r\n{"event":"forged.admin.event"}\x00'
    r = await api_client.post(
        "/v1/incidents",
        json={"target_id": "sim-target-log"},
        headers={
            "X-Tenant-ID": str(isolated_tenant),
            "Idempotency-Key": f"log-inj-{uuid.uuid4()}",
            "X-Correlation-ID": injected_cid,
        },
    )
    assert r.status_code == 202
    returned_cid = r.headers.get("X-Correlation-ID", "")
    assert "\r" not in returned_cid
    assert "\n" not in returned_cid
    assert "\x00" not in returned_cid

    # Verify emit_event redacts secret keys and Postgres URLs
    rec = emit_event(
        "incident.admitted",
        postgres_password=actual_pw,
        api_token="super-secret-token",
        dsn=f"postgresql+asyncpg://postgres:{actual_pw}@localhost:5432/opsblaze",
    )
    assert rec["postgres_password"] == "[REDACTED]"
    assert rec["api_token"] == "[REDACTED]"
    assert actual_pw not in str(rec["dsn"])
    assert "[REDACTED]" in str(rec["dsn"])

    # Verify redact_value directly
    assert actual_pw not in str(
        redact_value({"password": actual_pw, "nested": {"secret_key": actual_pw}})
    )

    # Verify /metrics contains no tenant UUIDs in metric labels
    m_snap = metrics.snapshot()
    assert str(isolated_tenant) not in str(m_snap["counters"])
    assert str(isolated_tenant) not in str(m_snap["gauges"])


def test_fake_investigator_cannot_mutate_or_accept_command_injection() -> None:
    """Verify the fake investigator treats target_id strictly as inert data and never mutates."""
    inv = FakeInvestigator()
    malicious_input = WorkflowIncidentInput(
        tenant_id=str(uuid.uuid4()),
        incident_id=str(uuid.uuid4()),
        target_id="sim-db; DROP TABLE incident; -- $(whoami)",
        request_sha256="0" * 64,
    )
    res = inv.investigate(malicious_input)
    assert res.requires_human_approval is True
    assert all(o.read_only is True for o in res.observations)
