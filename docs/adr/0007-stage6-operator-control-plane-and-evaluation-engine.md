# ADR-0007: Stage 6 — Operator Control Plane, Approval System, Trace Explorer & Evaluation Engine

* **Status:** Accepted (Hardened via EGM Goldfish Swarm: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`)
* **Date:** 2026-11-08
* **Authors:** OpsBlaze Architecture & Agentic AI Reliability Engineering
* **Capabilities Covered:**
  * `P2` — Version-Bound Approval Cockpit
  * `P1` — Live Investigation Workbench
  * `P3` — Evidence-to-Effect Trace Explorer
  * `S1` — Time-Sealed Paired Decision Replay & Statistical Evaluation Engine
* **Depends On:**
  * [`0001-b1-durable-incident-workflow.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md)
  * [`0002-e8-adapter-conformance-and-chaos-lab.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md)
  * [`0003-b2-version-fenced-resource-reservation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md)
  * [`0004-b3-effect-receipt-and-reconciliation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md)
  * [`0005-stage4-durable-integrity-and-cross-store-recovery.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md)
  * [`0006-stage5-adaptive-investigation-and-evidence-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md)

---

## 1. Context and Problem Statement

Following Stage 5, OpsBlaze possesses an adaptive investigation engine (A1/A2/A3/A4/B4) capable of discriminating root-cause hypotheses, bounding observations, collecting immutable evidence, and safely proposing structured remediation plans.
However, transitioning an incident from investigation to consequential action requires four critical control plane and evaluation foundations:
1. **P2 (Approval Cockpit):** Proposed actions cannot be executed on model authority. An approval must be a durable, typed PostgreSQL domain record bound to an exact canonical plan digest, target version, policy version, and authorized human reviewer identity. A chat button or model self-authorization is not an approval. Furthermore, B3 must recheck approval validity at dispatch time (`ActionGateway.execute_with_permit`).
2. **P1 (Investigation Workbench):** Incident responders require live situational awareness (hypotheses, evidence timeline, tool calls, budget consumption, abstention status). The client interface must rely on authoritative REST snapshots with ETag/versioning and lightweight SSE event notifications, enforcing stale-view protection and disabling sensitive controls during network disconnections or event gaps.
3. **P3 (Trace Explorer):** Explainability requires projecting causal lineage from observations to hypotheses, proposed plans, human approvals, reservations, effect receipts, and postcondition verifications. Lineage must be derived strictly from authoritative database rows, with missing links honestly visualized rather than hallucinated.
4. **S1 (Paired Decision Replay):** The exploratory 5-fixture Stage 5 benchmark must not be accepted as general proof. S1 establishes a rigorous, time-sealed experimental apparatus comparing Strategy A (Static Runbook), Strategy B (Cheapest-First), and Strategy C (A1 Adaptive Planner) under identical conditions, immutable frozen manifests, protected holdout labels, independent oracles, negative controls, and proper uncertainty metrics.

---

## 2. Non-Negotiable Invariants & Authority Boundaries

| Invariant ID | Rule Description | Enforcement Location |
|---|---|---|
| **INV-P2-1** | **Exact Plan Digest & Version Binding** | An approval record is permanently bound to `(tenant_id, incident_id, step_id, plan_id, canonical_plan_digest, action_digest, target_id, expected_resource_version, policy_version)`. Any divergence invalidates the approval. |
| **INV-P2-2** | **Strict Server-Side Human Authorization** | Reviewer identity is verified server-side. Forbidden roles (`agent`, `model`, `investigator`, `llm`, `system`, `autonomous_agent`) are rejected fail-closed. |
| **INV-P2-3** | **Two-Phase Dispatch Lease & Revalidation in B3** | In Short Tx 1, `approval_request` transitions `APPROVED -> DISPATCHING(lease)`. Never hold database locks across external network I/O. On Temporal activity retry of an existing receipt, allow idempotent completion. When postcondition verifies, transition `DISPATCHING -> CONSUMED`. |
| **INV-P2-4** | **Approval State Machine & Idempotent Signal** | Permitted transitions: `PENDING -> (APPROVED | REJECTED | CANCELLED | EXPIRED)`, `APPROVED -> (DISPATCHING | REVOKED | INVALIDATED | EXPIRED)`, `DISPATCHING -> (CONSUMED | REVOKED | INVALIDATED)`. Temporal approval signals use stable `approval_id` deduplication. |
| **INV-P1-1** | **Authoritative REST Snapshots with ETag & SSE Invalidation** | Primary source of truth is `GET /v1/incidents/{id}/workbench` with `ETag` (status_version + step/evidence count). SSE events stream monotonic `event_id` and pings (every 10s) without carrying large state payloads. |
| **INV-P1-2** | **Stale-View Control Guard & Server Fencing** | If snapshot age or SSE ping lag exceeds 30 seconds, UI disables action buttons. Server endpoint `POST /v1/approvals/{id}/approve` accepts `expected_incident_status_version: int` and rejects stale submissions with `409 Conflict`. |
| **INV-P3-1** | **Source-Backed Lineage Projection** | P3 nodes and edges are strictly generated from persisted PostgreSQL tables (`incident`, `investigation_step`, `evidence_ledger`, `tool_call`, `approval_request`, `resource_reservation`, `effect_receipt`). Direct foreign key `approval_request.step_id` ensures zero heuristic guesswork. |
| **INV-P3-2** | **Honest Missing Link Representation & Secret Redaction** | Broken or uncertain lineage chains are explicitly tagged `MISSING` or `NEEDS_REVIEW`. All trace node payloads returned to clients pass through `ObservationRedactor`. |
| **INV-S1-1** | **Immutable Evaluation Manifests & Natural Idempotency** | Evaluation runs are bound to frozen `EvaluationManifest` rows with canonical manifest hash. Unique index on `(manifest_id, pair_id, strategy_name)` ensures idempotent trial execution. |
| **INV-S1-2** | **Protected Holdout Label Isolation** | Ground-truth fault injections and root causes reside in `HiddenLabelStore`, inaccessible to candidate planners or investigator workers during evaluation. |
| **INV-S1-3** | **Independent Oracle & Negative Control Gating** | Negative control fixtures must result in `ABSTAINED` or `NEEDS_REVIEW`. Proposing a mutation on a negative control or an oracle false-success immediately invalidates the entire evaluation report. |
| **INV-S1-4** | **Paired Statistical Rigor** | Binary accuracy evaluated via McNemar's paired test; continuous metrics evaluated via Wilcoxon signed-rank test; Wilson score confidence intervals reported. |

---

## 3. Database Schema & Alembic Migration (`0004_stage6_p2_p3_s1.py`)

Stage 6 introduces 4 new tables in PostgreSQL:

### 3.1 `approval_request`
```sql
CREATE TABLE approval_request (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    incident_id UUID NOT NULL,
    step_id UUID REFERENCES investigation_step(id) ON DELETE SET NULL,
    plan_id VARCHAR(128) NOT NULL,
    canonical_plan_digest VARCHAR(64) NOT NULL,
    action_digest VARCHAR(64) NOT NULL,
    action_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_id VARCHAR(128) NOT NULL,
    expected_resource_version VARCHAR(64) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    reviewer_id VARCHAR(128),
    reviewer_role VARCHAR(64),
    expires_at TIMESTAMPTZ NOT NULL,
    decision_at TIMESTAMPTZ,
    reason TEXT,
    dispatch_lease_id UUID,
    dispatch_lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_approval_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_approval_state CHECK (
        state IN ('PENDING', 'APPROVED', 'DISPATCHING', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'CONSUMED')
    )
);
CREATE UNIQUE INDEX ux_active_approval_per_incident ON approval_request (tenant_id, incident_id)
    WHERE state IN ('PENDING', 'APPROVED', 'DISPATCHING');
CREATE INDEX ix_approval_lookup ON approval_request (tenant_id, incident_id, state);
CREATE INDEX ix_approval_expiry ON approval_request (state, expires_at) WHERE state IN ('PENDING', 'APPROVED', 'DISPATCHING');
```

### 3.2 `approval_audit`
```sql
CREATE TABLE approval_audit (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    approval_id UUID NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    reason TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_approval_audit_approval FOREIGN KEY (approval_id)
        REFERENCES approval_request(id) ON DELETE RESTRICT,
    CONSTRAINT chk_approval_audit_action CHECK (
        action IN ('CREATED', 'APPROVED', 'REJECTED', 'REVOKED', 'INVALIDATED', 'EXPIRED', 'DISPATCHED', 'CONSUMED')
    )
);
CREATE INDEX ix_approval_audit_lookup ON approval_audit (tenant_id, approval_id, timestamp);
```

### 3.3 `evaluation_manifest`
```sql
CREATE TABLE evaluation_manifest (
    id UUID PRIMARY KEY,
    manifest_sha256 VARCHAR(64) UNIQUE NOT NULL,
    title VARCHAR(256) NOT NULL,
    fixture_family VARCHAR(128) NOT NULL,
    strategy_version VARCHAR(64) NOT NULL,
    budget_config JSONB NOT NULL,
    oracle_version VARCHAR(64) NOT NULL,
    random_seed INTEGER NOT NULL,
    split VARCHAR(32) NOT NULL DEFAULT 'DEV',
    frozen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_manifest_split CHECK (split IN ('DEV', 'VALIDATION', 'HOLDOUT', 'NEGATIVE_CONTROL'))
);
```

### 3.4 `evaluation_trial`
```sql
CREATE TABLE evaluation_trial (
    id UUID PRIMARY KEY,
    manifest_id UUID NOT NULL REFERENCES evaluation_manifest(id) ON DELETE CASCADE,
    pair_id UUID NOT NULL,
    strategy_name VARCHAR(64) NOT NULL,
    fixture_id VARCHAR(128) NOT NULL,
    is_negative_control BOOLEAN NOT NULL DEFAULT FALSE,
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    outcome VARCHAR(64),
    decision_correct BOOLEAN,
    probes_count INTEGER NOT NULL DEFAULT 0,
    tokens_consumed INTEGER NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    raw_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT ux_trial_manifest_pair_strategy UNIQUE (manifest_id, pair_id, strategy_name),
    CONSTRAINT chk_trial_status CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED', 'INVALID'))
);
CREATE INDEX ix_eval_trial_manifest ON evaluation_trial (manifest_id, strategy_name);
```

---

## 4. API & Interface Specifications

### 4.1 P2 Approval Cockpit Endpoints
* `POST /v1/plans/{id}/approvals`: Create a pending approval request bound to the current proposed plan, step, target version, and action digest.
* `GET /v1/approvals/{id}`: Retrieve approval status, digest, reviewer, and expiration.
* `POST /v1/approvals/{id}/approve`: Submit human approval. Validates reviewer role, checks expiration, compares `expected_incident_status_version`, advances state `PENDING -> APPROVED`, writes audit record, and signals Temporal `approve_and_execute`.
* `POST /v1/approvals/{id}/reject`: Human reviewer explicitly rejects plan (`PENDING -> REJECTED`), signaling Temporal `resolve_awaiting_approval` with `NEEDS_REVIEW`.
* `POST /v1/approvals/{id}/revoke`: Revoke an existing `APPROVED` decision before dispatch (`APPROVED -> REVOKED`), signaling Temporal `revoke_approval`.

### 4.2 P1 Investigation Workbench Endpoints
* `GET /v1/incidents/{id}/workbench`: Comprehensive REST snapshot returning incident status, current hypotheses, verified evidence items, tool call audit log, remaining budget headroom, abstention details, active approval request, projection lag, and `ETag`. Supports `If-None-Match -> 304 Not Modified`.
* `GET /v1/incidents/{id}/events`: SSE event notification channel streaming monotonic `event_id`, timestamps, invalidation markers, and 10s keepalive pings (`event: ping`).

### 4.3 P3 Trace Explorer Endpoints
* `GET /v1/incidents/{id}/trace`: Returns server-projected Directed Acyclic Graph (DAG) consisting of typed `TraceNode` and `TraceEdge` items, with missing link annotations and secret-redacted payloads.

---

## 5. Temporal Workflow & B3 Integration Flow

```
IncidentWorkflow (AWAITING_APPROVAL)
       |
       +---> [P2 ApprovalService.approve()]
       |         |
       |         +---> PostgreSQL: approval_request -> APPROVED (audit logged)
       |         +---> Temporal Signal: approve_and_execute(ApprovedExecutionSignalInput)
       |
       v
IncidentWorkflow receives approve_and_execute
       |
       +---> Transition status -> EXECUTING
       +---> Execute Activity: stage3_execute_approved_action
                 |
                 +---> ApprovalService.claim_dispatch_lease() (Short Tx 1)
                 |         - Lock approval_request under FOR UPDATE
                 |         - Verify state IN ('APPROVED', 'DISPATCHING')
                 |         - Verify expires_at > NOW()
                 |         - Verify canonical_plan_digest and action_digest match
                 |         - Verify expected_resource_version == resource_epoch.version_seen
                 |         - Transition state -> DISPATCHING (lease_expires_at = now + 60s)
                 |         - Commit Tx 1
                 |
                 +---> B2 Acquire Monotonic Fence & Active Reservation
                 +---> B3 Insert EffectReceipt (PREPARED -> SENT)
                 +---> Adapter.apply(mutation) (External Network I/O - NO DB LOCKS HELD)
                 +---> Postcondition Verification (CONFIRMED != VERIFIED)
                 |
                 +---> ApprovalService.finalize_consumed() (Short Tx 2)
                           - Transition state -> CONSUMED
                           - Commit Tx 2
```
