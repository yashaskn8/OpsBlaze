# Runbook: Experience Consent Management, Privacy Revocation & Payload Retention

**Feature:** E1 — Consent-Scoped Experience Capsule  
**Version:** 1.0.0  
**Date:** 2026-11-08  
**Scope:** Tenant privacy consent lifecycle, deterministic scrubbing, synchronous invalidation, and retention purge.

---

## 1. Architecture Overview & Core Invariants

The Experience Capsule subsystem captures incident investigation traces (symptoms, hypotheses, observed evidence manifests, effect receipts, and outcome links) to inform future investigations and evaluation benchmarks.

Non-negotiable security and privacy invariants:
1. **Query-Time Invalidation (`INV-E1-1`):** Physical deletion is asynchronous, but retrieval eligibility is synchronous. All read paths join `experience_capsule` against `consent_epoch` with `consent_epoch.reuse_allowed = TRUE AND experience_capsule.consent_epoch = consent_epoch.current_epoch`. Revoking consent immediately makes all existing capsules ineligible with 0ms delay.
2. **Deterministic Pre-Storage Redaction (`INV-E1-2`):** Raw prompts, tokens, credentials, and passwords are never stored in `summary_markdown`. All text passes through `ExperienceRedactor` (Unicode NFKC normalization, regex scrubbing of JWTs, Bearer tokens, AWS keys, passwords, database URLs, and 4096-byte bounds).
3. **Non-Interfering Projector Boundary (`INV-E1-3`):** Experience projection errors never fail or block the incident workflow. Exceptions are caught, logged, and increment `experience_projection_error_total`.
4. **Irreversible Payload Erasure (`INV-E1-4`):** Once a capsule is tombstoned or purged, `summary_markdown` is set to `NULL` permanently and cannot be revived by subsequent projections.

---

## 2. API Reference & Operations

### 2.1 Query Current Consent Status
```http
GET /v1/privacy/experience-consent
X-Tenant-ID: <tenant-uuid>
```
**Response (200 OK):**
```json
{
  "tenant_id": "11111111-1111-4111-8111-111111111111",
  "current_epoch": 1,
  "reuse_allowed": true,
  "updated_at": "2026-11-08T08:00:00Z",
  "revoked_at": null
}
```

### 2.2 Revoke Consent (Privacy / GDPR Right to be Forgotten)
```http
POST /v1/privacy/experience-consent
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "reuse_allowed": false,
  "reason": "Tenant requested immediate data exclusion",
  "actor_id": "operator-alice",
  "actor_role": "incident_commander"
}
```
**Behavior:**
- Atomically advances `current_epoch = current_epoch + 1`.
- Sets `reuse_allowed = FALSE` and records `revoked_at`.
- Emits structured event `experience.consent.revoked`.
- All historical capsules for this tenant immediately disappear from `/v1/experiences`.

### 2.3 Grant or Restore Consent
```http
POST /v1/privacy/experience-consent
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "reuse_allowed": true,
  "reason": "Tenant accepted telemetry terms",
  "actor_id": "operator-alice",
  "actor_role": "incident_commander"
}
```
**Behavior:**
- Increments epoch, sets `reuse_allowed = TRUE`.
- New capsules created under the new epoch become eligible. Past capsules from prior epochs remain ineligible.

### 2.4 Quarantine a Specific Capsule
```http
POST /v1/experiences/{capsule_id}/quarantine
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "reason": "Operational context contains proprietary internal hostname",
  "actor_id": "lead-sre",
  "actor_role": "incident_commander",
  "expected_lifecycle_state": "ELIGIBLE"
}
```

---

## 3. Scheduled Worker & Retention Procedures

The background worker `RetentionWorker` handles two types of cleanup:
1. **TTL Purging:** Capsules with `expires_at <= NOW()` (default 90-day retention) have their `summary_markdown` set to `NULL`, state set to `PURGED`, and an audit row recorded.
2. **Revocation Purging:** Capsules with `consent_epoch < current_epoch` or whose tenant has `reuse_allowed = FALSE` are asynchronously cleared.

### Triggering Retention Worker Manually
```python
from backend.experience.retention import RetentionWorker
from backend.persistence.db import make_engine, make_session_factory
from backend.config import get_settings

engine = make_engine(get_settings())
session_factory = make_session_factory(engine)
worker = RetentionWorker(session_factory)

# Purge expired TTLs across all tenants
purged_count = await worker.purge_expired_capsules(batch_size=100)

# Purge revoked capsules for a specific tenant
purged_revoked = await worker.purge_revoked_capsules(tenant_id, batch_size=100)
```

---

## 4. Telemetry & Alerting

| Metric / Event | Type | Alert Threshold | Description |
|---|---|---|---|
| `experience_consent_revoked_total` | Counter | Info | Tenant consent revocation counter |
| `experience_capsule_purged_total` | Counter | Info | Capsules whose payloads were scrubbed |
| `experience_projection_error_total` | Counter | > 5 per minute | Non-blocking projection failures |
| `experience_redaction_reject_total` | Counter | > 1 per minute | Malformed/unredactable payloads |
