# ADR-0006: Stage 5 — Adaptive Investigation, Read-Only Intelligence & Evidence Engine

* **Status:** Accepted (Hardened via EGM Goldfish Swarm: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`)
* **Date:** 2026-11-07
* **Authors:** OpsBlaze Architecture & Agentic AI Reliability Engineering
* **Capabilities Covered:**
  * `A2` — Capability-Gated Read-Only Tools
  * `A3` — Versioned Evidence-Context Ledger
  * `B4` — Durable Budget & Capacity Primitives for Adaptive Investigation
  * `A1` — Budgeted Adaptive Probe Planner
  * `A4` — Calibrated Abstention and Escalation
* **Depends On:** [0001-b1-durable-incident-workflow.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md), [0002-e8-adapter-conformance-and-chaos-lab.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md), [0003-b2-version-fenced-resource-reservation.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md), [0004-b3-effect-receipt-and-reconciliation.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md), [0005-stage4-durable-integrity-and-cross-store-recovery.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md)

---

## 1. Context and Problem Statement

In OpsBlaze (TITAN-20), incident investigation requires collecting discriminating observations from monitored systems, diagnosing underlying root causes, and formulating candidate remediation plans under strict cost, time, and safety budgets.
Standard agentic architectures suffer from severe systemic failure modes:
1. **Unchecked Tool Authority:** Language models given raw API keys or open tool execution run arbitrary scripts, trigger unauthorized mutations, or exceed target system rate limits.
2. **Context Poisoning & Hallucinated Evidence:** Agents cite fabricated logs, fail to record provenance, overwrite earlier contradictory evidence, or ingest poisoned tool outputs (prompt injection via logs or runbooks).
3. **Runaway Resource Consumption:** Investigation loops get stuck in infinite querying loops, over-consuming tokens and overloading degraded target systems during live incidents.
4. **False Confidence & Silent Guesses:** Language models fabricate high certainty on ambiguous data, leading to dangerous actions without human review.
5. **Non-Deterministic Replay Corruption:** Multi-turn model conversations inside durable workflow engines break deterministic history replay upon worker restart or recovery.

Stage 5 establishes an **evidence-driven, bounded, server-authorized investigation engine** where the model acts purely as an untrusted reasoning component that selects hypotheses and discriminating probes, while deterministic backend services strictly govern capabilities, evidence immutability, budgets, and escalation gates.

---

## 2. Non-Negotiable Authority, Security & Trust Invariants

| ID | Invariant | Enforcement Mechanism |
|---|---|---|
| **INV-A2-1** | **Strict Server-Side Read-Only Allowlist** | A2 is an isolated capability gateway. No mutation tools exist in A2. The model cannot execute unregistered tools, arbitrary shell commands, or arbitrary HTTP requests. |
| **INV-A2-2** | **Tenant & Target-Scoped Capability Grants** | Tool execution requires a valid, unexpired, unrevoked `CapabilityGrant` minted by the deterministic backend for `(tenant_id, incident_id, tool_id, target_id)`. Target credentials exist only in the trusted gateway worker environment, never in model prompts or frontend context. |
| **INV-A2-3** | **Bounded Outputs & Secret Redaction** | All tool outputs are strictly bounded (max 64 KB). Semantic bounding wraps truncated outputs in a structured envelope (`{"truncated": true, "byte_length": ..., "sample": [...]}`). High-entropy strings matching secret patterns (tokens, API keys, `.env` values) are stripped by `SecretRedactor` before hashing and persistence. |
| **INV-A2-4** | **Prompt Injection Isolation** | Untrusted tool outputs, stack traces, and runbook extracts are structurally fenced inside `<untrusted_observation id="..."> ... </untrusted_observation>` tags before model context construction. System instructions mandate treating observation text purely as inert diagnostic data. |
| **INV-A3-1** | **Immutable Append-Only Evidence Provenance** | Evidence records in `evidence_ledger` are strictly append-only. Each record captures `(id, tenant_id, incident_id, tool_call_id, source, source_version, target_id, target_version, observed_at, captured_at, sha256_digest)`. Evidence rows are never updated in place. Superseding observations reference older rows via `supersedes_id UUID REFERENCES evidence_ledger(id)`. |
| **INV-A3-2** | **Version-Pinned Context Manifests** | Every model inference step is bound to a single cohesive `investigation_step` record in PostgreSQL capturing prompt digest, model ID, evidence UUIDs, runbook revision, and token count. Replay and audit can bit-identically reconstruct what the model saw. |
| **INV-B4-1** | **PostgreSQL Authoritative Budget Reservation & Settlement** | Budgets (tokens, probe calls, time, cost) are reserved in PostgreSQL *before* calling model or tool APIs, and settled *afterwards* with exact reservation delta reclamation. Crashes, restarts, or retries cannot obtain free quota or exceed incident/tenant budget caps. |
| **INV-A1-1** | **Structured Hypothesis Separation Loop** | Investigation is a single deterministic loop: `UNTRIAGED -> HYPOTHESIZING -> PROBING -> REASSESSING -> (PROBING | PROPOSED | ABSTAINED)`. The model outputs typed Pydantic structures evaluating explicit competing hypotheses. |
| **INV-A1-2** | **Deterministic Temporal Workflow Replay (`workflow.patched`)** | `IncidentWorkflow` preserves `workflow.patched("stage5-adaptive-investigation-v1")` with chronological fallback to legacy B1 investigation for pre-Stage 5 histories. In-flight activity restarts resume idempotently from the latest completed `step_no` in PostgreSQL. |
| **INV-A4-1** | **Three-Gate Decoupled Abstention & Proposal** | Decouples probe continuation from proposal and abstention. At Step 1, low confidence margin triggers probe continuation, not premature abstention. Escalation to `NEEDS_REVIEW` occurs only when budgets are exhausted, probes are depleted, or contradictory evidence persists after maximum probes. |
| **INV-A4-2** | **Zero Model Self-Authorization** | The model may draft a typed proposed plan for future P2 review, but cannot approve it or execute consequential actions. P2 approval remains missing and non-bypassable. |

---

## 3. Component Architecture & Data Flow

```
+---------------------------------------------------------------------------------------------------+
|                                  TEMPORAL INCIDENT WORKFLOW (B1)                                  |
|                                                                                                   |
|  1. Transition status -> INVESTIGATING                                                            |
|  2. If workflow.patched("stage5-adaptive-investigation-v1"):                                      |
|        Execute Activity: RunAdaptiveInvestigationActivity (A1 Loop with heartbeat & resume)       |
|     Else:                                                                                         |
|        Execute Legacy Activity: b1_run_fake_investigation (Preserved for Stage 1-4 replay)        |
|                                                                                                   |
|     +------------------------------------------------------------------------------------------+  |
|     |                         A1 ADAPTIVE INVESTIGATOR (Reasoning Loop)                        |  |
|     |                                                                                          |  |
|     |  Startup: Query PostgreSQL for max completed step_no. Resume at step k = max(step_no)+1. |  |
|     |  Loop while k <= max_probes:                                                             |  |
|     |    a. Heartbeat step_no to Temporal.                                                     |  |
|     |    b. Atomically Reserve Budget (B4) in PostgreSQL: tokens, probe slot, cost with lease. |  |
|     |    c. Build Context & Pin Manifest (A3): ContextBuilder with inert untrusted fencing.    |  |
|     |    d. Model Completion: Rank Hypotheses & Select Discriminating Probe                   |  |
|     |    e. Check CapabilityGrant & Dispatch Read-Only Adapter (A2):                           |  |
|     |       - Verify grant unexpired & unrevoked                                               |  |
|     |       - Enforce semantic bounds (max 64KB) & Redact Secrets                             |  |
|     |       - Record tool_call row in PostgreSQL                                               |  |
|     |    f. Append Immutable Evidence (A3) with sha256_digest & tool_call_id.                  |  |
|     |    g. Settle Budget (B4): subtract reserved delta, add actual tokens/cost in PostgreSQL. |  |
|     |    h. Reassess Hypotheses: update posteriors based on fresh observation.                 |  |
|     |    i. Persist unified investigation_step (A3/A1) in PostgreSQL.                          |  |
|     |    j. Evaluate Uncertainty Gates (A4):                                                   |  |
|     |       - PROPOSAL GATE: confidence_margin >= 0.30 & coverage >= 0.75 -> Exit to PROPOSED   |  |
|     |       - ABSTENTION GATE: budget exhausted OR candidate probes empty -> Exit to ABSTAINED |  |
|     |       - CONTINUATION GATE: continue probing -> k = k + 1                                 |  |
|     +------------------------------------------------------------------------------------------+  |
|                                                                                                   |
|  3. Complete Activity with InvestigationResult (Hypotheses, Evidence IDs, Proposal/Abstention)    |
|  4. If Proposed -> Project status to AWAITING_APPROVAL (P2 Cockpit boundary)                      |
|  5. If Abstained/Handoff -> Project status to NEEDS_REVIEW (Human intervention)                   |
+---------------------------------------------------------------------------------------------------+
```

---

## 4. Hardened PostgreSQL Schema (Alembic Migration: `stage5_001_a1_a2_a3_a4_b4`)

All tables enforce composite foreign keys for strict multi-tenant isolation:
`ForeignKeyConstraint(["tenant_id", "incident_id"], ["incident.tenant_id", "incident.id"], ondelete="CASCADE")`.

### 4.1 A2 Tool Catalog & Grants
```sql
CREATE TABLE tool_catalog (
    id VARCHAR(64) NOT NULL,
    version VARCHAR(32) NOT NULL,
    input_schema_hash VARCHAR(64) NOT NULL,
    output_schema_hash VARCHAR(64) NOT NULL,
    read_only BOOLEAN NOT NULL DEFAULT TRUE,
    max_cost_micros INT NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    egress_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, version)
);

CREATE TABLE capability_grant (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    tool_id VARCHAR(64) NOT NULL,
    tool_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    target_id VARCHAR(64) NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT fk_capability_grant_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_capability_grant_tool FOREIGN KEY (tool_id, tool_version)
        REFERENCES tool_catalog(id, version) ON DELETE RESTRICT
);
CREATE INDEX ix_capability_grant_lookup ON capability_grant(tenant_id, incident_id, tool_id, target_id);

CREATE TABLE tool_call (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    step_no INT NOT NULL,
    grant_id UUID NOT NULL REFERENCES capability_grant(id) ON DELETE RESTRICT,
    tool_id VARCHAR(64) NOT NULL,
    tool_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    target_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL, -- PENDING, COMPLETED, FAILED, TIMED_OUT, REJECTED
    args_digest VARCHAR(64) NOT NULL,
    args_payload JSONB NOT NULL,
    output_digest VARCHAR(64),
    result_evidence_id UUID,
    duration_ms FLOAT NOT NULL DEFAULT 0.0,
    error_detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_tool_call_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_tool_call_incident ON tool_call(tenant_id, incident_id, step_no);
```

### 4.2 A3 Evidence Ledger & Unified Investigation Step
```sql
CREATE TABLE evidence_ledger (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    tool_call_id UUID REFERENCES tool_call(id) ON DELETE SET NULL,
    source VARCHAR(64) NOT NULL,
    source_version VARCHAR(32) NOT NULL,
    target_id VARCHAR(64) NOT NULL,
    target_version VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sha256_digest VARCHAR(64) NOT NULL,
    raw_payload_redacted JSONB NOT NULL,
    classification VARCHAR(32) NOT NULL, -- TELEMETRY, LOG_SAMPLE, CONFIG_SNAPSHOT, RUNBOOK_EXTRACT
    validity_status VARCHAR(32) NOT NULL DEFAULT 'VALID', -- VALID, STALE, CORRUPTED, SUPERSEDED, UNAVAILABLE
    supersedes_id UUID REFERENCES evidence_ledger(id) ON DELETE SET NULL,
    CONSTRAINT fk_evidence_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_evidence_lookup ON evidence_ledger(tenant_id, incident_id, validity_status);

CREATE TABLE investigation_step (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    step_no INT NOT NULL,
    loop_state VARCHAR(32) NOT NULL, -- HYPOTHESIZING, PROBING, REASSESSING, PROPOSED, ABSTAINED
    prompt_digest VARCHAR(64) NOT NULL,
    evidence_ids_used JSONB NOT NULL DEFAULT '[]'::jsonb,
    token_count INT NOT NULL DEFAULT 0,
    hypotheses_json JSONB NOT NULL,
    selected_probe JSONB,
    proposed_plan_json JSONB,
    abstention_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_investigation_step_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_investigation_step UNIQUE (tenant_id, incident_id, step_no)
);
```

### 4.3 B4 Budget Ledger
```sql
CREATE TABLE budget_ledger (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    incident_id UUID NOT NULL,
    reserved_tokens INT NOT NULL DEFAULT 0,
    settled_tokens INT NOT NULL DEFAULT 0,
    reserved_probes INT NOT NULL DEFAULT 0,
    settled_probes INT NOT NULL DEFAULT 0,
    reserved_cost_micros INT NOT NULL DEFAULT 0,
    settled_cost_micros INT NOT NULL DEFAULT 0,
    max_tokens INT NOT NULL DEFAULT 50000,
    max_probes INT NOT NULL DEFAULT 10,
    max_cost_micros INT NOT NULL DEFAULT 10000000, -- $10.00
    max_time_seconds INT NOT NULL DEFAULT 300,
    reservation_nonce UUID,
    lease_expires_at TIMESTAMPTZ,
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, EXHAUSTED, SETTLED
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_budget_ledger_incident FOREIGN KEY (tenant_id, incident_id)
        REFERENCES incident(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_budget_ledger_incident UNIQUE (tenant_id, incident_id)
);
```

---

## 5. Detailed Component Specifications

### 5.1 A2: Capability-Gated Read-Only Tools
- **Default Catalog Seed:**
  1. `telemetry_sample`: queries metrics/telemetry around error spikes (`read_only=True`).
  2. `log_query`: extracts recent structured log entries matching error signatures (`read_only=True`).
  3. `config_read`: reads declared configuration and environment flags for a target (`read_only=True`).
  4. `health_check`: queries HTTP/gRPC health probe status and latency (`read_only=True`).
- **Semantic Bounding & Envelope:** If payload exceeds 64 KB, output is sliced at logical item boundaries and returned in a typed envelope: `{"truncated": true, "byte_length": len, "items": [...]}`.
- **Secret Redaction:** `SecretRedactor` masks API keys, bearer tokens, passwords, and `.env` credentials with `[REDACTED_SECRET]` before hashing and database insertion.

### 5.2 A3: Versioned Evidence-Context Ledger
- **Append-Only Immutability:** Older records are never modified. If a new observation replaces an older one, the new record stores `supersedes_id = older_row.id`.
- **Evidence TTLs:**
  - `TELEMETRY`: 120 seconds TTL
  - `LOG_SAMPLE`: 300 seconds TTL
  - `CONFIG_SNAPSHOT`: 600 seconds TTL
  - `RUNBOOK_EXTRACT`: 3600 seconds TTL
- **Prompt Data Fencing:** `ContextManifestBuilder` formats inert data blocks inside XML tags (`<untrusted_observation id="...">`) so indirect prompt injections cannot hijack model control flow.

### 5.3 B4: Two-Phase Budget Reservation & Settlement
```python
async def reserve_budget(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    tokens: int,
    probes: int,
    cost_micros: int,
    lease_seconds: float = 30.0,
) -> uuid.UUID:
    """Atomically reserve quota in PostgreSQL with lease expiry."""
    # SELECT FOR UPDATE budget_ledger
    # Verify: (settled_tokens + reserved_tokens + tokens <= max_tokens)
    # Update reserved_* += requested, set lease_expires_at, mint reservation_nonce

async def settle_budget(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    nonce: uuid.UUID,
    reserved_tokens: int,
    reserved_probes: int,
    reserved_cost_micros: int,
    actual_tokens: int,
    actual_probes: int,
    actual_cost_micros: int,
) -> None:
    """Atomically settle actual usage and release unspent reservation delta."""
    # SELECT FOR UPDATE budget_ledger
    # Verify nonce matches (or lease unexpired)
    # reserved_tokens -= reserved_tokens
    # settled_tokens += actual_tokens
    # updated_at = now()
```

### 5.4 A1: Budgeted Adaptive Probe Planner
- **Pydantic Hypotheses:**
  ```python
  class HypothesisItem(BaseModel):
      id: str
      title: str
      probability: float
      supporting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)
      contradicting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)

  class HypothesisEvaluation(BaseModel):
      hypotheses: list[HypothesisItem]
      confidence_margin: float
      selected_probe: ProbeSelection | None = None
      proposed_plan: ProposedPlan | None = None
  ```
- **Probe Selection:** Model evaluates candidate discriminating probes and returns a typed `ProbeSelection` (`tool_id`, `target_id`, `parameters`, `rationale`).
- **Deterministic Fake Model Adapter:** For offline deterministic tests, `DeterministicFakeModelAdapter` maps incident fixtures to scripted hypothesis updates and probe selections.

### 5.5 A4: Three-Gate Calibrated Abstention Engine
- **Gate 1: Proposal Gate (Success Exit)**:
  `confidence_margin >= 0.30` AND `evidence_coverage >= 0.75` AND `proposed_plan IS NOT NULL`.
  Transition to `AWAITING_APPROVAL`.
- **Gate 2: Continuation Gate (Keep Probing)**:
  `step_no < max_probes` AND `budget_remaining > 0` AND candidate probes available.
  Continue to step $k+1$.
- **Gate 3: Abstention Gate (Escalate to Human)**:
  `budget_exhausted` OR `candidate_probes_empty` OR `conflicting_evidence_unresolvable`.
  Record `EscalationReason` (`BUDGET_EXHAUSTED`, `INSUFFICIENT_EVIDENCE`, etc.) and transition to `NEEDS_REVIEW`.

---

## 6. Omega Baseline Comparison Specification (Phase 10)

Benchmark comparing 3 strategies across 5 identical held-out incident fixtures:
1. **Strategy A (Static Runbook):** Fixed sequential probes (`health_check` -> `config_read` -> `telemetry_sample`).
2. **Strategy B (Cheapest-First Heuristic):** Selects cheapest probe until budget exhausted.
3. **Strategy C (A1 Adaptive Planner):** Hypothesis-driven discriminating probe selection with calibrated abstention.

Evaluation Metrics:
- **Diagnosis Accuracy:** Fraction of incidents correctly diagnosed against ground truth oracle.
- **Unsafe Proposal Rate:** Fraction of proposals recommending incorrect actions.
- **Average Probes per Incident:** Probe count required to reach resolution.
- **Total Token & Cost Efficiency:** Total tokens and cost incurred per correct diagnosis.
- **Correct Abstention Rate:** Clean escalation to human handoff on ambiguous fixtures.
