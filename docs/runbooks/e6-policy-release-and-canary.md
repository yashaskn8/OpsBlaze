# Runbook: E6 Policy Release & Canary Deployment Operations

**Product:** OpsBlaze / TITAN-20  
**Feature:** E6 — Versioned Policy Canary, Release Control & Automated Rollback  
**Target Roles:** Release Owner, Incident Commander, Platform Engineer, SRE Lead, On-Call Engineer  
**Status:** Active  
**Last Updated:** 2026-11-08  

---

## 1. Overview & Architecture Invariants

Feature E6 provides governed, safe, statistically monitored deployment of evaluated policy improvements through immutable signed policy bundles, mandatory human sign-off, deterministic cohort routing, and runtime safety gates.

### Non-Negotiable Invariants

1. **INV-E6-1 (Bundle Cryptographic Sealing & Immutability):** Policy bundles are locked into immutable artifacts via RFC 8785 JSON Canonicalization, SHA-256 content hashing, and HMAC-SHA256 signatures with domain separation (`opsblaze:policy_bundle:v1:{key_id}:{digest}`). Unsealed bundles cannot be released. Tampering after sealing invalidates verification.
2. **INV-E6-2 (Governed 8-State Release Lifecycle):** Releases advance strictly through the explicit finite state machine:
   `DRAFT -> PENDING_APPROVAL -> APPROVED -> SHADOW -> CANARY -> ACTIVE -> ROLLED_BACK / REVOKED`.
3. **INV-E6-3 (Strict Separation of Duties):** The human operator who created the release draft (`drafter_id`) is strictly forbidden from approving it (`reviewer_id <> drafter_id`). Attempted self-approval returns HTTP 403 Forbidden.
4. **INV-E6-4 (Human-Only Approval Authority):** Only verified human principals with `release_owner` or `incident_commander` roles may approve releases. Autonomous agents, models, and bots are forbidden from release approval.
5. **INV-E6-5 (Source E5 Eligibility Check):** Approving a release dynamically re-validates the candidate's underlying E5 evaluation report. The report must be `PASS`, promotion-eligible, and not superseded by subsequent holdout evaluations.
6. **INV-E6-6 (Anti-Split-Brain Single Active Release):** Only one release per `(tenant_id, target_component)` may exist in `SHADOW`, `CANARY`, or `ACTIVE` state simultaneously, enforced by database partial unique index `uq_active_release_per_target`.
7. **INV-E6-7 (Deterministic Cohort Partitioning):** Canary assignment uses deterministic SHA-256 consistent hashing over `{tenant_id}:{target_id}:{rollout_salt}:{incident_id}` modulo 100. Repeat queries for the same incident always yield identical cohort arms.
8. **INV-E6-8 (Atomic Incident Policy Pinning):** Admitted incidents are atomically pinned to their assigned bundle and release directly on `incident.pinned_policy_bundle_id` and `incident.canary_arm`. In-flight investigations are never hot-swapped mid-flight.

---

## 2. Release State Machine

```
              ┌─────────┐
              │  DRAFT  │
              └────┬────┘
                   │ submit_for_approval (v1 -> v2)
                   ▼
       ┌───────────────────────┐
       │   PENDING_APPROVAL    ├──────────────┐
       └───────────┬───────────┘              │ reject_release (REVOKED)
                   │ review_release (v2 -> v3)│
                   ▼                          ▼
             ┌───────────┐               ┌─────────┐
             │ APPROVED  │               │ REVOKED │ (Terminal)
             └─────┬─────┘               └─────────┘
                   │
         ┌─────────┴─────────┐
         │ activate_shadow   │ activate_canary
         ▼                   ▼
    ┌──────────┐        ┌──────────┐
    │  SHADOW  ├───────►│  CANARY  │
    └────┬─────┘promote └────┬─────┘
         │                   │ promote_active (100%)
         │                   ▼
         │              ┌──────────┐
         │              │  ACTIVE  │
         │              └────┬─────┘
         │                   │
         └─────────┬─────────┘
                   │ trigger_rollback / guardrail / kill-switch
                   ▼
            ┌─────────────┐
            │ ROLLED_BACK │ (Terminal)
            └─────────────┘
```

---

## 3. Standard Operating Procedures

### 3.1 Sealing a Policy Bundle

Draft bundles generated from approved E5 regression evaluations must be cryptographically sealed before deployment:

```bash
curl -X POST "http://localhost:8000/v1/bundles/<BUNDLE_UUID>/seal" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <AUTH_TOKEN>"
```

### 3.2 Bootstrapping Baseline Incumbent Policy

If no sealed incumbent bundle exists for a target component, bootstrap the v1.0.0 baseline:

```bash
curl -X POST "http://localhost:8000/v1/bundles/bootstrap?target_component=investigator" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <AUTH_TOKEN>"
```

### 3.3 Drafting and Submitting a Release

```bash
# 1. Create draft release (Version 1)
curl -X POST "http://localhost:8000/v1/releases" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <DRAFTER_TOKEN>" \
  -d '{
    "target_component": "investigator",
    "bundle_id": "<CANDIDATE_BUNDLE_UUID>",
    "incumbent_bundle_id": "<INCUMBENT_BUNDLE_UUID>"
  }'

# 2. Submit for approval (Version 1 -> Version 2)
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/submit" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <DRAFTER_TOKEN>" \
  -d '{"expected_version": 1, "target_state": "PENDING_APPROVAL"}'
```

### 3.4 Reviewing and Approving a Release

Mandatory human sign-off with distinct `release_owner` or `incident_commander`:

```bash
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/review" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <REVIEWER_TOKEN>" \
  -d '{
    "expected_version": 2,
    "decision": "APPROVED",
    "decision_comments": "Passed offline E5 paired trials with zero safety regressions."
  }'
```

### 3.5 Activating Shadow Mode

In Shadow mode, candidate observations are generated asynchronously alongside the live incumbent baseline with zero live operational impact (`canary_percentage = 0`):

```bash
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/activate-shadow" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <RELEASE_OWNER_TOKEN>" \
  -d '{"expected_version": 3, "target_state": "SHADOW"}'
```

### 3.6 Activating Canary Deployment & Traffic Stepping

Step canary traffic progressively (e.g., 10% -> 25% -> 50%):

```bash
# Initial 10% canary
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/activate-canary" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <RELEASE_OWNER_TOKEN>" \
  -d '{"expected_version": 4, "target_state": "CANARY", "canary_percentage": 10}'

# Step up to 25%
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/update-canary" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <RELEASE_OWNER_TOKEN>" \
  -d '{"expected_version": 5, "target_state": "CANARY", "canary_percentage": 25}'
```

### 3.7 Promoting to 100% Active Production Policy

Once canary evaluation criteria are satisfied across target workloads:

```bash
curl -X POST "http://localhost:8000/v1/releases/<RELEASE_UUID>/promote-active" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <RELEASE_OWNER_TOKEN>" \
  -d '{"expected_version": 6, "target_state": "ACTIVE"}'
```

---

## 4. Audit Trail & Verification

Inspect the append-only audit log for full compliance verification:

```bash
curl -X GET "http://localhost:8000/v1/releases/<RELEASE_UUID>/audits" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -H "Authorization: Bearer <AUTH_TOKEN>"
```
