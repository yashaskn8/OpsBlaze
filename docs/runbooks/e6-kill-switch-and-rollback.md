# Runbook: E6 Safety Guardrails, Automated Rollback & Emergency Kill-Switch

**Product:** OpsBlaze / TITAN-20  
**Feature:** E6 — Versioned Policy Canary, Release Control & Automated Rollback  
**Target Roles:** SRE Lead, On-Call Engineer, Incident Commander, Platform Engineer, Operator  
**Status:** Active  
**Last Updated:** 2026-11-08  

---

## 1. Overview & Safety Architecture

Feature E6 implements defense-in-depth safety boundaries to guarantee that candidate policy defects cannot cause cascading failures, unauthorized resource mutations, or persistent service degradation.

### Two-Tier Automated Safety Guardrails

| Tier | Trigger Condition | Evaluation Cadence | Action Taken |
|---|---|---|---|
| **Class 1 (Zero-Tolerance Invariant)** | $N = 1$ occurrence of unauthorized mutation, negative-control breach, or B2 fencing violation. | Instantaneous on event detection | Immediate automated atomic rollback (`ROLLBACK_TRIGGERED`) |
| **Class 2 (Statistical Rate Metric)** | Evaluated after $N_{\min} \ge 30$ candidate incidents: Candidate Error Rate $> 5.0\%$ **AND** Relative Risk vs concurrent Incumbent $RR > 1.5$. | Periodic / On-demand guardrail check | Automated atomic rollback with statistical attribution |

### The Emergency Kill-Switch

The Kill-Switch is a dedicated operator failsafe designed to instantly halt candidate policy execution without requiring workflow termination or database restarts:
1. **Instantaneous Traffic Reversion:** New incident admissions immediately fall back to the baseline incumbent bundle (`canary_arm = 'INCUMBENT'`).
2. **Consequential Mutation Fencing:** In-flight incidents previously assigned to the candidate arm are strictly fenced server-side. Attempts to acquire B2 version-fenced reservations or dispatch P2 approval effects are blocked, transitioning the incident to `ESCALATED_TO_HUMAN`.
3. **Immutable Audit Record:** Every kill-switch invocation requires a recorded justification string and identifies the human operator in `release_event_audit`.

---

## 2. Emergency Operational Procedures

### 2.1 Tripping the Emergency Kill-Switch via CLI

Any authorized human operator (`operator`, `sre_lead`, `release_owner`, `incident_commander`, `platform_engineer`) can trip the kill-switch:

```bash
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/kill-switch" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <OPERATOR_TOKEN>" \
  -d '{
    "expected_version": <CURRENT_VERSION>,
    "justification": "CRITICAL: Candidate exhibiting high latency and memory spikes under load"
  }'
```

**Expected Response (HTTP 200 OK):**
```json
{
  "id": "<RELEASE_UUID>",
  "state": "ROLLED_BACK",
  "canary_percentage": 0,
  "kill_switch_active": true,
  "kill_switch_triggered_by": "sre-lead-01",
  "rollback_reason": "KILL_SWITCH: CRITICAL: Candidate exhibiting high latency and memory spikes under load"
}
```

### 2.2 Triggering Manual Controlled Rollback

For non-emergency deprecations or expected rollbacks:

```bash
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/rollback" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <OPERATOR_TOKEN>" \
  -d '{
    "expected_version": <CURRENT_VERSION>,
    "rollback_reason": "Canary error rate elevated above target SLO"
  }'
```

### 2.3 Evaluating Canary Health & Guardrail Status

To inspect live error rates, sample sizes, and relative risk:

```bash
# 1. Inspect live metrics
curl -X GET "http://localhost:8000/v1/releases/<RELEASE_UUID>/metrics" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <AUTH_TOKEN>"

# 2. Trigger active guardrail evaluation
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/guardrail-check" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <AUTH_TOKEN>" \
  -d '{"simulated_invariant_breaches": 0}'
```

---

## 3. Post-Rollback Procedure & Incident Triage

1. **Verify Live Traffic Routing:** Verify via workbench and database that all new incident admissions are assigned `canary_arm = 'INCUMBENT'`.
2. **Review Fenced Incidents:** Check for incidents in `ESCALATED_TO_HUMAN` state that were in-flight during the kill-switch activation.
3. **Capture Telemetry:** Export release audit events via `GET /v1/releases/<RELEASE_UUID>/audits` for the post-incident review.
4. **Invalidate Candidate Evaluation Report:** If the candidate policy is fundamentally flawed, record a formal dispute or superseding evaluation report in E5.
