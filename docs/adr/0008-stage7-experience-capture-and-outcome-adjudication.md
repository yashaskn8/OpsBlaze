# ADR-0008: Milestone 7 — Governed Experience Capture (E1) & Independent Outcome Adjudication Ledger (E2)

* **Status:** Accepted (Architectural Baseline for Stage 8 / Milestone 7)
* **Date:** 2026-11-08
* **Authors:** OpsBlaze Architecture & Agentic AI Reliability Engineering
* **Capabilities Covered:**
  * `E1` — Consent-Scoped Experience Capsule
  * `E2` — Independent Outcome Adjudication Ledger
* **Depends On:**
  * [`0001-b1-durable-incident-workflow.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md)
  * [`0002-e8-adapter-conformance-and-chaos-lab.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md)
  * [`0003-b2-version-fenced-resource-reservation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md)
  * [`0004-b3-effect-receipt-and-reconciliation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md)
  * [`0005-stage4-durable-integrity-and-cross-store-recovery.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md)
  * [`0006-stage5-adaptive-investigation-and-evidence-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md)
  * [`0007-stage6-operator-control-plane-and-evaluation-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md)

---

## 1. Context and Problem Statement

Following Stage 6, OpsBlaze features an adaptive investigation engine (A1–A4, B4), an operator control plane (P1–P3), and a time-sealed paired decision replay framework (S1).
However, closing the operational feedback loop safely requires two critical learning and evaluation primitives:
1. **E1 (Experience Capsule):** Capturing minimal, privacy-bounded representations of incidents for future offline learning, evaluation, and policy analysis. Crucially, experiences must never be raw transcript dumps; they must be consent-governed, tenant-isolated, redacted, and immediately revokable. E1 must operate asynchronously and must never block or alter B1 incident progress.
2. **E2 (Outcome Adjudication Ledger):** In an agentic incident recovery system, an agent's self-reported success or an adapter's HTTP 200 is NOT proof that the workload recovered. E2 separates agent assertions, adapter acknowledgements, and independent workload observations into an append-only adjudication ledger with immutable revisions.

Neither E1 nor E2 grants an autonomous agent authority to approve plans, alter production code, or self-grade outcomes.

---

## 2. Non-Negotiable Invariants & Authority Boundaries

| Invariant ID | Rule Description | Enforcement Location |
|---|---|---|
| **INV-E1-1** | **Default Consent Denied & Epoch Invalidation** | A tenant defaults to `reuse_allowed = FALSE`. Every capsule records the `consent_epoch` under which it was captured. Advancing the epoch immediately invalidates all prior capsules from query/export eligibility at query time, even before physical purge. Re-enabling consent never resurrects tombstoned capsules. |
| **INV-E1-2** | **Strict Server-Side Derivation & Redaction** | Experience capsules derive all fields from server-side domain records. A1 models cannot submit capsules. Redaction occurs before reusable storage. Unredactable or malformed payloads fail closed to `QUARANTINED`. Raw prompts, transcripts, or unredacted tool outputs are forbidden. |
| **INV-E1-3** | **Non-Blocking Capture & Idempotency** | Experience capture failure must never block B1 incident completion or modify terminal state. Projector is at-least-once with natural idempotency on `(tenant_id, incident_id, step_id, schema_version)`. |
| **INV-E1-4** | **Auditable Tombstone & Purge Lifecycle** | Purged/revoked records transition to `TOMBSTONED` and have sensitive payloads erased (`summary_markdown = NULL`). A separate `experience_audit` ledger records lifecycle events without leaking payload. |
| **INV-E2-1** | **Separation of Concerns: Adapter ACK vs Independent Truth** | An adapter ACK (`ADAPTER_ACK`) indicates mutation dispatch, NOT verified workload recovery. Verified success requires designated independent black-box observations (`INDEPENDENT_WORKLOAD_CHECK`). |
| **INV-E2-2** | **Immutable Observations & Revisions** | `outcome_observation` and `outcome_revision` are strictly append-only. A new observation or correction creates revision $N+1$ referencing prior revision $N$. Previous revisions are never updated or deleted. |
| **INV-E2-3** | **Human Reviewer Authority & CAS Concurrency** | Only authorized human roles (`incident_commander`, `platform_engineer`, `sre_lead`) can submit feedback or resolve disputes. Autonomous models cannot resolve disputes or mint trusted labels. Updates enforce optimistic concurrency (`expected_revision_no` / `If-Match`). |
| **INV-E2-4** | **Negative Control Truth Gate** | Negative control incidents must result in `NO_OP`. Any attempt to adjudicate a mutation on a negative control as verified success fails the quality gate immediately. |
| **INV-E2-5** | **Dependent Record Invalidation** | When an outcome is revised (e.g., delayed relapse after earlier success), dependent experience capsules are flagged stale to prevent contaminated training or evaluation data. |

---

## 3. Database Schema Design

Stage 7 introduces 5 additive PostgreSQL tables:

### 3.1 `consent_epoch`
```sql
CREATE TABLE consent_epoch (
    tenant_id UUID PRIMARY KEY REFERENCES tenant(id) ON DELETE RESTRICT,
    current_epoch INT NOT NULL DEFAULT 1,
    reuse_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
```

### 3.2 `experience_capsule`
```sql
CREATE TABLE experience_capsule (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    incident_id UUID NOT NULL,
    step_id UUID REFERENCES investigation_step(id) ON DELETE SET NULL,
    schema_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    consent_epoch INT NOT NULL,
    policy_bundle_version VARCHAR(64) NOT NULL DEFAULT 'v1',
    model_id VARCHAR(128) NOT NULL,
    model_version VARCHAR(64) NOT NULL,
    evidence_manifest_sha256 VARCHAR(64) NOT NULL,
    effect_receipt_id UUID REFERENCES effect_receipt(id) ON DELETE SET NULL,
    outcome_id UUID,
    outcome_revision_no INT,
    redaction_version VARCHAR(32) NOT NULL DEFAULT 'redact-v1',
    redacted_digest VARCHAR(64) NOT NULL,
    summary_markdown TEXT,
    classification VARCHAR(64) NOT NULL,
    lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'CAPTURED',
    occurred_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_experience_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_experience_idempotency UNIQUE (tenant_id, incident_id, step_id, schema_version),
    CONSTRAINT chk_experience_state CHECK (
        lifecycle_state IN ('CAPTURED', 'ELIGIBLE', 'QUARANTINED', 'TOMBSTONED', 'PURGED')
    )
);
CREATE INDEX ix_experience_eligible ON experience_capsule (tenant_id, consent_epoch, lifecycle_state)
    WHERE lifecycle_state = 'ELIGIBLE';
CREATE INDEX ix_experience_expiry ON experience_capsule (lifecycle_state, expires_at);
CREATE INDEX ix_experience_lookup ON experience_capsule (tenant_id, incident_id);
```

### 3.3 `experience_audit`
```sql
CREATE TABLE experience_audit (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    capsule_id UUID NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    reason TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_experience_audit_lookup ON experience_audit (tenant_id, capsule_id, timestamp);
```

### 3.4 `outcome_observation`
```sql
CREATE TABLE outcome_observation (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    incident_id UUID NOT NULL,
    source_kind VARCHAR(64) NOT NULL,
    source_reference VARCHAR(256) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rule_version VARCHAR(64) NOT NULL,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_digest VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_outcome_obs_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_outcome_obs_idempotency UNIQUE (tenant_id, incident_id, idempotency_key)
);
CREATE INDEX ix_outcome_obs_lookup ON outcome_observation (tenant_id, incident_id, observed_at);
```

### 3.5 `outcome_revision`
```sql
CREATE TABLE outcome_revision (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    incident_id UUID NOT NULL,
    revision_no INT NOT NULL,
    rule_version VARCHAR(64) NOT NULL,
    label VARCHAR(32) NOT NULL,
    confidence_band VARCHAR(16) NOT NULL DEFAULT 'LOW',
    dispute_reason TEXT,
    prior_revision_id UUID REFERENCES outcome_revision(id) ON DELETE SET NULL,
    reviewer_id VARCHAR(128),
    reviewer_role VARCHAR(64),
    supporting_observation_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    adjudication_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_outcome_rev_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_outcome_rev_incident_rev UNIQUE (incident_id, revision_no),
    CONSTRAINT chk_outcome_label CHECK (
        label IN ('PENDING', 'OBSERVED', 'VERIFIED_SUCCESS', 'VERIFIED_FAILURE', 'DISPUTED', 'REVISED', 'UNKNOWN')
    )
);
CREATE INDEX ix_outcome_rev_lookup ON outcome_revision (tenant_id, incident_id, revision_no DESC);
```

---

## 4. State Machines & Progression Workflows

### 4.1 E1 Capsule Lifecycle
```
[Observed Domain Events]
        │
        ▼
   POLICY_CHECKED ─── (Consent Denied) ───► QUARANTINED
        │
        ▼
     REDACTED
        │
        ▼
     CAPTURED ──── (Consent Epoch Matches) ────► ELIGIBLE
        │                                         │
        ├──────────────── (Quarantine) ───────────┤
        │                                         ▼
        │                                    QUARANTINED
        │                                         │
        ▼                                         ▼
   TOMBSTONED ◄── (Consent Revoked / Purged) ─────┘
   (Payload Erased: summary=NULL, audit logged)
```

### 4.2 E2 Adjudication Lifecycle
```
       PENDING (Waiting for evidence)
          │
          ├───────────► OBSERVED (Adapter ACK only, or partial readback)
          │
          ├─── (Independent Check Passes) ────────► VERIFIED_SUCCESS
          │
          ├─── (Independent Check Fails) ─────────► VERIFIED_FAILURE
          │
          └─── (Conflicting Evidence) ────────────► DISPUTED
                                                       │
                     ┌─────────────────────────────────┴──────────────────┐
                     ▼                                                    ▼
                  REVISED                                             UNRESOLVED
     (Human Reviewer Adjudication)                       (Escalated, no consensus)
```

---

## 5. Security, Privacy & Least Privilege

1. **Deterministic Redaction Before Persistence:** All text written to `summary_markdown` passes through `ExperienceRedactor` scrubbing auth tokens, API keys, JWTs, and passwords.
2. **Query-Time Invalidation Gate:** `SELECT ... FROM experience_capsule c JOIN consent_epoch e ON c.tenant_id = e.tenant_id WHERE e.reuse_allowed = TRUE AND c.consent_epoch = e.current_epoch AND c.lifecycle_state = 'ELIGIBLE'`. Revoking consent makes all existing capsules immediately ineligible.
3. **No Autonomous Self-Grading:** Agents and models are strictly forbidden from writing `outcome_revision` with `VERIFIED_SUCCESS`. Reviewer endpoints require verified human roles (`incident_commander`, `platform_engineer`, `sre_lead`).
4. **Optimistic Concurrency Control:** Consequential corrections use `If-Match: "{revision_no}"` to prevent lost updates during concurrent operator reviews.
