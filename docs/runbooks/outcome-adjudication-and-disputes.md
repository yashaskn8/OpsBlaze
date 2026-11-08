# Runbook: Outcome Adjudication, Independent Verification & Dispute Resolution

**Feature:** E2 — Independent Outcome Adjudication Ledger  
**Version:** 1.0.0  
**Date:** 2026-11-08  
**Scope:** Automated observation ingest, black-box verification rules, human dispute adjudication, and CAS concurrency control.

---

## 1. Architecture Overview & Core Invariants

The Outcome Adjudication Ledger maintains immutable, append-only records of incident resolution observations and versioned adjudication revisions.

Non-negotiable invariants:
1. **Decoupled Verification (`INV-E2-1`):** Adapter execution acknowledgements (`ADAPTER_ACK`) and model summaries (`MODEL_ANNOTATION`) can NEVER produce a `VERIFIED_SUCCESS` revision. At most they produce `OBSERVED` with low confidence.
2. **Independent Black-Box Authority (`INV-E2-2`):** `VERIFIED_SUCCESS` strictly requires an independent, external workload or synthetic health probe (`INDEPENDENT_WORKLOAD_CHECK`) passing.
3. **Negative Control Zero-Tolerance (`INV-E2-3`):** If an independent probe fails or detects an injected fault, the state MUST transition to `VERIFIED_FAILURE`. 0% false success is permitted.
4. **Relapse & Conflict Detection (`INV-E2-4`):** If a delayed monitor (`DELAYED_MONITOR`) reports failure after apparent success, or if observations conflict, the state transitions to `DISPUTED`.
5. **CAS Optimistic Concurrency (`INV-E2-5`):** Human operator feedback requires `expected_revision_no`. Stale updates fail with `409 Conflict`.
6. **Self-Grading Prohibition (`INV-E2-6`):** Autonomous agent/model self-grading is strictly blocked. Attempting to record success from `MODEL_ANNOTATION` raises `SelfGradingForbiddenError` (`403 Forbidden`).

---

## 2. API Reference & Operations

### 2.1 Ingest Automated Observation
```http
POST /v1/incidents/{incident_id}/outcome/observations
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "source_kind": "INDEPENDENT_WORKLOAD_CHECK",
  "source_reference": "synthetics:orders-health",
  "rule_version": "v1",
  "result_payload": {
    "status": "PASS",
    "latency_ms": 35,
    "http_code": 200
  },
  "idempotency_key": "obs-orders-20261108-01"
}
```
**Response (201 Created):**
```json
{
  "observation": {
    "id": "22222222-2222-4222-8222-222222222222",
    "source_kind": "INDEPENDENT_WORKLOAD_CHECK",
    "source_digest": "..."
  },
  "current_revision": {
    "revision_no": 2,
    "label": "VERIFIED_SUCCESS",
    "confidence_band": "HIGH"
  }
}
```

### 2.2 Submit Human Feedback / Resolve Dispute
```http
POST /v1/incidents/{incident_id}/outcome/feedback
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "label": "VERIFIED_SUCCESS",
  "reason": "Confirmed manual canary orders processed correctly without duplicate charges",
  "actor_id": "alice-lead",
  "actor_role": "incident_commander",
  "expected_revision_no": 2
}
```

---

## 3. Operator Dispute Resolution Runbook

When an incident status displays `DISPUTED` in the P1 Workbench or P3 Trace:

1. **Inspect Observations:**
   ```bash
   curl -H "X-Tenant-ID: $TENANT_ID" http://localhost:8000/v1/incidents/$INCIDENT_ID/outcome
   ```
   Look at `total_observations` and find conflicting sources (e.g. Adapter claimed restart succeeded, but synthetic probe timed out, or delayed monitor caught a crashloop relapse).

2. **Verify Target Telemetry:**
   Review metrics, pod logs, and external dashboards outside the agent loop.

3. **Adjudicate Revision:**
   - If workload recovered: Submit `POST .../feedback` with `label="VERIFIED_SUCCESS"` and `expected_revision_no`.
   - If workload is still degraded: Submit `POST .../feedback` with `label="VERIFIED_FAILURE"` and escalate to platform team.
   - If concurrency conflict (`409 Conflict`) occurs: Re-fetch `GET .../outcome` to inspect the concurrent operator's revision, and retry with updated `expected_revision_no`.

---

## 4. Telemetry & Monitoring

| Metric / Event | Type | Alert Threshold | Description |
|---|---|---|---|
| `outcome_observation_recorded_total` | Counter | Info | Append-only observations received |
| `outcome_disputed_total` | Counter | > 0 per hour | Relapse or conflict detections |
| `outcome_self_grading_blocked_total` | Counter | > 0 | Blocked model self-grading attempts |
| `outcome_operator_feedback_total` | Counter | Info | Human dispute resolution decisions |
