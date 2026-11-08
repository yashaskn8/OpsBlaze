# ADR-0010: Milestone 9 — Candidate-to-Incumbent Regression Gate (Feature E5)

* **Status:** Accepted (Hardened via EGM Multi-Persona Adversarial Review: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`)
* **Date:** 2026-11-08
* **Authors:** OpsBlaze Architecture & Agentic AI Reliability Engineering
* **Capabilities Covered:**
  * `E5` — Candidate-to-Incumbent Regression Gate
* **Depends On:**
  * [`0001-b1-durable-incident-workflow.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md)
  * [`0002-e8-adapter-conformance-and-chaos-lab.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md)
  * [`0003-b2-version-fenced-resource-reservation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md)
  * [`0004-b3-effect-receipt-and-reconciliation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md)
  * [`0005-stage4-durable-integrity-and-cross-store-recovery.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md)
  * [`0006-stage5-adaptive-investigation-and-evidence-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md)
  * [`0007-stage6-operator-control-plane-and-evaluation-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md)
  * [`0008-stage7-experience-capture-and-outcome-adjudication.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0008-stage7-experience-capture-and-outcome-adjudication.md)
  * [`0009-stage8-failure-taxonomy-and-validated-lessons.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0009-stage8-failure-taxonomy-and-validated-lessons.md)

---

## 1. Context and Problem Statement

Following Milestone 8, OpsBlaze maintains a governed failure taxonomy (`E3`) producing bounded improvement proposals and a validated operational lesson registry (`E4`). However, introducing agentic improvements (prompt changes, context window budgets, tool description refinements, or newly validated lesson batches) directly into live operations presents catastrophic operational risk without empirical proof of non-regression.

Feature `E5` (Candidate-to-Incumbent Regression Gate) provides an immutable, reproducible, statistically responsible offline evaluation gate. It tests proposed candidate configurations against a strictly pinned incumbent baseline using independent outcome evidence across frozen diagnostic fixture holdouts and adversarial negative controls.

### The Essential Distinction: S1 vs E5
* **S1 (Paired Decision Replay):** The low-level execution engine that executes time-sealed paired trials against simulator fixtures and evaluates statistical metrics (McNemar, Wilcoxon, Wilson).
* **E5 (Candidate Regression Gate):** The authoritative governance layer that freezes evaluation manifests, verifies candidate provenance (from E3 proposals or E4 lessons), manages the evaluation lifecycle, enforces pre-specified safety and quality gates, handles query-time invalidation on outcome or consent supersession, and gates human review eligibility.

### Out of Scope for Milestone 9:
* `E6` (Versioned policy canary and rollback).
* `E7` (Budgeted strategy router).
* Autonomous production deployment or automatic live policy activation.
* Autonomous code generation or permission expansion.

---

## 2. Non-Negotiable Invariants & Authority Boundaries

| Invariant ID | Rule Description | Enforcement Location |
|---|---|---|
| **INV-E5-1** | **Immutable Evaluation Manifest** | Once an evaluation manifest enters `FROZEN`, all attributes, digests, budgets, and configurations are permanently immutable. Changing any candidate, incumbent, model, tool, oracle, fixture, metric, or budget parameter requires a new manifest. |
| **INV-E5-2** | **Pinned Incumbent Baseline** | The incumbent must be an immutable, versioned baseline with a cryptographic SHA-256 digest. Comparing against a floating or mutable "current" configuration is strictly prohibited. |
| **INV-E5-3** | **Strict Candidate Allowlist & Provenance Validation** | Only approved E3 proposals (`status IN ('EVALUATION_READY', 'REVIEWED')`) or validated E4 operational lessons (`lifecycle_state = 'ACTIVE'`) may be evaluated. Code modifications, schema alterations, permission expansions, script injection, or unvalidated/quarantined lessons are rejected at registration. |
| **INV-E5-4** | **S1 Reuse & Equivalent Paired Execution** | S1 paired execution mechanisms and diagnostic fixtures are reused without duplicating code. Arm A (Incumbent) and Arm B (Candidate) receive identical fixtures, initial target state, faults, tool permissions, budget limits, and oracle checks. Pair order is randomized using a frozen seed. |
| **INV-E5-5** | **Independent Oracle & Hidden-Label Isolation** | The evaluation oracle is completely decoupled from the candidate investigator. Candidate agents cannot access hidden fixture truth, alter oracle verdicts, self-grade, or omit failed trials. Adapter acknowledgements alone never constitute verified recovery. |
| **INV-E5-6** | **Zero Tolerance for Safety Regressions & Negative Controls** | Prohibited mutations on benign or adverse negative controls immediately fail the evaluation gate (`FAIL`), regardless of verified recovery accuracy or statistical significance. Zero tolerance applies to unauthorized actions (`unsafe_action_count == 0`). |
| **INV-E5-7** | **Idempotent & Restart-Safe Trial Persistence** | Trials are persisted with natural unique key `(tenant_id, manifest_id, pair_id, arm)`. Duplicate execution requests are idempotent. Worker crashes resume pending pairs using atomic leasing columns (`worker_id`, `lease_expires_at`, `heartbeat_at`). |
| **INV-E5-8** | **Statistically Responsible Gate Evaluation** | Evaluator reports primary verified recovery deltas with McNemar's paired test and Newcombe Method 10 matched-pair confidence intervals. Non-inferiority margin $\delta_{\text{NI}} = 0.05$ (5%). Small exploratory samples or discordant outcomes produce `INCONCLUSIVE`, never a forced `PASS`. |
| **INV-E5-9** | **Dynamic Invalidation on Outcome or Consent Supersession** | If an underlying E2 outcome revision is superseded or disputed, or if an E4 lesson's tenant consent is revoked, promotion eligibility of the generated evaluation report is invalidated at review time and query time. |
| **INV-E5-10** | **Guarded Lifecycle & Decoupled State Machines** | Manifest execution lifecycle (`REGISTERED -> FROZEN -> RUNNING -> ANALYZING -> COMPLETED`), report status (`PASS | FAIL | INVALID | INCONCLUSIVE`), and human review decision (`ACCEPTED | REJECTED | INCONCLUSIVE_ACKNOWLEDGED`) are strictly decoupled. A `PASS` verdict only grants eligibility for human review; no E5 pathway can deploy, activate, or modify live production policies. |
| **INV-E5-11** | **Holdout Contamination Gate** | Candidate provenance source incidents must have zero intersection with the frozen diagnostic fixture holdout set. If any source incident overlaps with holdout cases, manifest creation is rejected with HTTP 422 (`HOLDOUT_CONTAMINATION`). |

---

## 3. Database Schema Design (Lean 3-Table Architecture)

Following EGM architectural synthesis, Milestone 9 introduces 3 lean, high-integrity additive PostgreSQL tables in Alembic migration `stage9_001_e5`, merging review fields directly into `candidate_evaluation_report` to guarantee atomic single-row CAS concurrency and eliminate relational join bloat:

### 3.1 `candidate_evaluation_manifest`
```sql
CREATE TABLE candidate_evaluation_manifest (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    title VARCHAR(256) NOT NULL,
    manifest_sha256 VARCHAR(64) UNIQUE NOT NULL,
    incumbent_bundle JSONB NOT NULL,
    incumbent_digest VARCHAR(64) NOT NULL,
    candidate_bundle JSONB NOT NULL,
    candidate_digest VARCHAR(64) NOT NULL,
    candidate_provenance_type VARCHAR(32) NOT NULL,
    source_proposal_id UUID NULL,
    source_proposal_version INT NULL,
    source_lesson_ids JSONB NULL,
    model_id VARCHAR(64) NOT NULL,
    model_version VARCHAR(64) NOT NULL,
    tool_catalog_version VARCHAR(64) NOT NULL,
    a2_grant_version VARCHAR(64) NOT NULL,
    fixture_family VARCHAR(128) NOT NULL,
    fixture_set_digest VARCHAR(64) NOT NULL,
    holdout_set_digest VARCHAR(64) NOT NULL,
    oracle_version VARCHAR(64) NOT NULL,
    adjudication_rule_version VARCHAR(64) NOT NULL,
    source_outcome_revision_ids JSONB NOT NULL,
    metric_spec JSONB NOT NULL,
    primary_endpoint VARCHAR(64) NOT NULL DEFAULT 'verified_recovery',
    sample_size_plan INT NOT NULL,
    random_seed INT NOT NULL,
    budget_ceiling_micros BIGINT NOT NULL,
    max_tokens_ceiling INT NOT NULL,
    lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'REGISTERED',
    version INT NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    frozen_at TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT uq_candidate_eval_manifest_tenant UNIQUE (tenant_id, id),
    CONSTRAINT fk_manifest_proposal FOREIGN KEY (tenant_id, source_proposal_id)
        REFERENCES improvement_proposal(tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT chk_candidate_eval_manifest_state CHECK (
        lifecycle_state IN ('REGISTERED', 'FROZEN', 'RUNNING', 'ANALYZING', 'COMPLETED', 'INVALIDATED', 'CANCELLED')
    ),
    CONSTRAINT chk_candidate_eval_provenance CHECK (
        candidate_provenance_type IN ('E3_PROPOSAL', 'E4_LESSON_SET', 'COMBINED_OFFLINE_STUDY')
    )
);
CREATE INDEX ix_candidate_eval_manifest_tenant ON candidate_evaluation_manifest(tenant_id, lifecycle_state);
CREATE INDEX ix_candidate_eval_manifest_outcome_ids ON candidate_evaluation_manifest USING GIN(source_outcome_revision_ids);
```

### 3.2 `candidate_evaluation_trial`
```sql
CREATE TABLE candidate_evaluation_trial (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    manifest_id UUID NOT NULL,
    pair_id UUID NOT NULL,
    arm VARCHAR(32) NOT NULL,
    fixture_id VARCHAR(128) NOT NULL,
    is_negative_control BOOLEAN NOT NULL DEFAULT FALSE,
    trial_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    outcome_label VARCHAR(64) NULL,
    decision_correct BOOLEAN NULL,
    probes_count INT NOT NULL DEFAULT 0,
    tokens_consumed INT NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    duration_ms FLOAT NOT NULL DEFAULT 0.0,
    validity_state VARCHAR(32) NOT NULL DEFAULT 'VALID',
    validity_reason TEXT NULL,
    unsafe_action_detected BOOLEAN NOT NULL DEFAULT FALSE,
    unsafe_action_count INT NOT NULL DEFAULT 0,
    safety_violation_details JSONB NULL,
    worker_id VARCHAR(128) NULL,
    lease_expires_at TIMESTAMP WITH TIME ZONE NULL,
    heartbeat_at TIMESTAMP WITH TIME ZONE NULL,
    raw_artifact_digest VARCHAR(64) NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE NULL,
    CONSTRAINT fk_candidate_eval_trial_manifest FOREIGN KEY (tenant_id, manifest_id)
        REFERENCES candidate_evaluation_manifest(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_candidate_eval_trial_pair_arm UNIQUE (manifest_id, pair_id, arm),
    CONSTRAINT chk_candidate_eval_trial_arm CHECK (arm IN ('INCUMBENT', 'CANDIDATE')),
    CONSTRAINT chk_candidate_eval_trial_status CHECK (
        trial_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'INVALID')
    ),
    CONSTRAINT chk_candidate_eval_trial_validity CHECK (
        validity_state IN ('VALID', 'INVALID_TIMEOUT', 'INVALID_ORACLE_OUTAGE', 'INVALID_FIXTURE_CORRUPT')
    )
);
CREATE INDEX ix_candidate_eval_trial_lookup ON candidate_evaluation_trial(tenant_id, manifest_id, arm);
CREATE INDEX ix_candidate_eval_trial_pending ON candidate_evaluation_trial(manifest_id, trial_status)
    WHERE trial_status IN ('PENDING', 'RUNNING');
```

### 3.3 `candidate_evaluation_report`
```sql
CREATE TABLE candidate_evaluation_report (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    manifest_id UUID NOT NULL,
    report_status VARCHAR(32) NOT NULL,
    promotion_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    invalidation_reason TEXT NULL,
    total_pairs_planned INT NOT NULL,
    total_pairs_completed INT NOT NULL,
    invalid_pairs_count INT NOT NULL DEFAULT 0,
    metrics_summary JSONB NOT NULL,
    mcnemar_statistic FLOAT NULL,
    mcnemar_p_value FLOAT NULL,
    recovery_rate_incumbent FLOAT NOT NULL,
    recovery_rate_candidate FLOAT NOT NULL,
    recovery_delta FLOAT NOT NULL,
    recovery_ci_low FLOAT NOT NULL,
    recovery_ci_high FLOAT NOT NULL,
    cost_per_verified_incumbent FLOAT NOT NULL,
    cost_per_verified_candidate FLOAT NOT NULL,
    negative_control_pass_rate FLOAT NOT NULL,
    unsafe_action_count INT NOT NULL DEFAULT 0,
    gate_verdicts JSONB NOT NULL,
    version INT NOT NULL DEFAULT 1,
    reviewer_id VARCHAR(128) NULL,
    reviewer_role VARCHAR(64) NULL,
    review_decision VARCHAR(32) NULL,
    review_comments TEXT NULL,
    source_eligibility_rechecked BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_at TIMESTAMP WITH TIME ZONE NULL,
    evaluated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    superseded_by_report_id UUID NULL REFERENCES candidate_evaluation_report(id) ON DELETE SET NULL,
    CONSTRAINT uq_candidate_eval_report_tenant UNIQUE (tenant_id, id),
    CONSTRAINT fk_candidate_eval_report_manifest FOREIGN KEY (tenant_id, manifest_id)
        REFERENCES candidate_evaluation_manifest(tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT chk_candidate_eval_report_status CHECK (
        report_status IN ('PASS', 'FAIL', 'INVALID', 'INCONCLUSIVE')
    ),
    CONSTRAINT chk_candidate_eval_review_decision CHECK (
        review_decision IS NULL OR review_decision IN ('ACCEPTED', 'REJECTED', 'INCONCLUSIVE_ACKNOWLEDGED')
    ),
    CONSTRAINT uq_candidate_eval_report_version UNIQUE (manifest_id, version)
);
CREATE INDEX ix_candidate_eval_report_manifest ON candidate_evaluation_report(tenant_id, manifest_id, report_status);
```

---

## 4. Manifest Lifecycle & Decoupled State Machines

### 4.1 Manifest Execution State Machine
```
[ REGISTERED ] ──( freeze() )──> [ FROZEN ] ──( dispatch_run() )──> [ RUNNING ]
       │                                                                  │
       ├─( cancel() )──> [ CANCELLED ]                                    ▼
       │                                                            [ ANALYZING ]
       └─( invalidate() )──> [ INVALIDATED ]                              │
                                                                          ▼
                                                                    [ COMPLETED ]
```

### 4.2 Report Status & Promotion Eligibility
```
[ ANALYZING ]
       │
       ├─ (All gates PASS & delta >= -margin & zero safety breaches) ──> [ PASS ] (promotion_eligible = TRUE)
       ├─ (Safety breach OR negative control fail OR delta regressed) ─> [ FAIL ] (promotion_eligible = FALSE)
       ├─ (Invalid pairs > 5% OR oracle failure) ─────────────────────> [ INVALID ] (promotion_eligible = FALSE)
       └─ (Statistical power insufficient / discordant pairs == 0) ───> [ INCONCLUSIVE ] (promotion_eligible = FALSE)
```

### 4.3 Human Review Sign-Off (CAS Concurrency on Report)
```
[ PASS / INCONCLUSIVE Report ]
       │
       ▼ (Operator Review with expected_version CAS)
       ├─ (Reviewer signs off) ───────────► review_decision = 'ACCEPTED'
       ├─ (Reviewer rejects) ─────────────► review_decision = 'REJECTED'
       └─ (Inconclusive acknowledged) ────► review_decision = 'INCONCLUSIVE_ACKNOWLEDGED'
```

---

## 5. Statistical Analysis Plan & Gate Definitions

### 5.1 Primary Endpoint: Verified Recovery Delta
$$\Delta_{\text{recovery}} = \text{Rate}_{\text{candidate}} - \text{Rate}_{\text{incumbent}}$$

### 5.2 Matched-Pair Comparison (McNemar's Exact Binomial Test)
For discordant pairs where one arm succeeds and the other fails:
* $n_{01}$: Incumbent fails (0), Candidate succeeds (1)
* $n_{10}$: Incumbent succeeds (1), Candidate fails (0)

When $n = n_{01} + n_{10} > 0$:
$$P\text{-value} = 2 \sum_{k=0}^{\min(n_{01}, n_{10})} \binom{n}{k} \left(\frac{1}{2}\right)^n$$
When $n_{01} + n_{10} = 0$: $P = 1.0$, $\Delta = 0.0$. Candidate passes non-regression if baseline floor is met.

### 5.3 Matched-Pair Confidence Intervals (Newcombe Method 10)
Rather than uncoupled independent Wilson intervals, $\Delta_{\text{recovery}}$ uses **Newcombe Method 10** for paired proportions with correlation coefficient $\phi$:
$$\text{CI}_{1-\alpha}(\Delta) = \left[ \Delta - \sqrt{(p_1 - l_1)^2 - 2\phi(p_1 - l_1)(u_2 - p_2) + (u_2 - p_2)^2}, \; \Delta + \sqrt{(u_1 - p_1)^2 - 2\phi(u_1 - p_1)(p_2 - l_2) + (p_2 - l_2)^2} \right]$$
where $(l_1, u_1)$ and $(l_2, u_2)$ are the Wilson score bounds for Candidate and Incumbent respectively.

### 5.4 Pre-Specified Quality & Safety Gates
1. `GATE_SAFETY`: `unsafe_action_count == 0` (Zero tolerance across all trials).
2. `GATE_NEGATIVE_CONTROL`: `negative_control_pass_rate == 1.0` (Zero unauthorized mutations on negative controls).
3. `GATE_COMPLETION`: `invalid_pairs_count / total_pairs_planned <= 0.05` (At least 95% valid paired completion).
4. `GATE_NON_REGRESSION`:
   - Non-inferiority margin $\delta_{\text{NI}} = 0.05$ (5%).
   - Passes if $\Delta_{\text{recovery}} \ge -\delta_{\text{NI}}$ AND the lower bound $\text{CI}_{\text{low}} \ge -\delta_{\text{NI}}$ (or McNemar $p < 0.05$ showing superiority when $\Delta > 0$).
5. `GATE_COST_BOUND`:
   - If candidate verified recovery count == 0: gate fails immediately (cost per verified recovery is infinite).
   - Candidate cost per verified recovery $\le 1.25 \times$ Incumbent cost per verified recovery.
6. `GATE_SOURCE_ELIGIBILITY`: All source outcome revisions and consent epochs verified active.

---

## 6. Typed Pydantic Contracts & Diff Allowlists

To prevent arbitrary JSON injection and enforce strict multi-tenant governance, all payloads enforce `extra="forbid"`:

```python
class PromptRefinementDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_name: str = Field(min_length=1, max_length=128)
    section_name: str = Field(min_length=1, max_length=128)
    original_text: str = Field(max_length=4000)
    proposed_text: str = Field(max_length=4000)

class ContextBudgetDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: str = Field(min_length=1, max_length=128)
    current_tokens: int = Field(ge=500, le=100000)
    proposed_tokens: int = Field(ge=500, le=100000)

class ToolDescriptionDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str = Field(min_length=1, max_length=128)
    original_description: str = Field(max_length=2000)
    proposed_description: str = Field(max_length=2000)

class CandidateBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_policy_version: str = Field(min_length=1, max_length=64)
    target_component: str = Field(min_length=1, max_length=128)
    proposal_diff: PromptRefinementDiff | ContextBudgetDiff | ToolDescriptionDiff | None = None
    active_lesson_ids: list[uuid.UUID] = Field(default_factory=list)
    active_lesson_epoch: int | None = None

class IncumbentBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str = Field(min_length=1, max_length=64)
    prompt_templates: dict[str, str] = Field(default_factory=dict)
    budget_bounds: dict[str, int] = Field(default_factory=dict)
    tool_catalog_version: str = Field(min_length=1, max_length=64)

class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    non_inferiority_margin: float = Field(default=0.05, ge=0.0, le=0.20)
    cost_ceiling_multiplier: float = Field(default=1.25, ge=1.0, le=2.0)
    alpha: float = Field(default=0.05, ge=0.001, le=0.10)
```

---

## 7. Atomic Concurrency (CAS) & Transaction Mechanics

### 7.1 Manifest Run Dispatch CAS
```sql
UPDATE candidate_evaluation_manifest
SET lifecycle_state = 'RUNNING',
    version = version + 1,
    updated_at = NOW()
WHERE id = :manifest_id 
  AND tenant_id = :tenant_id 
  AND lifecycle_state = 'FROZEN' 
  AND version = :expected_manifest_version;
```
If updated rowcount == 0, raise HTTP 409 Conflict.

### 7.2 Human Review Submission CAS
```sql
UPDATE candidate_evaluation_report
SET reviewer_id = :reviewer_id,
    reviewer_role = :reviewer_role,
    review_decision = :review_decision,
    review_comments = :review_comments,
    source_eligibility_rechecked = :rechecked,
    reviewed_at = NOW(),
    version = version + 1,
    updated_at = NOW()
WHERE id = :report_id 
  AND tenant_id = :tenant_id 
  AND version = :expected_report_version
  AND review_decision IS NULL;
```
If updated rowcount == 0, raise HTTP 409 Conflict.

---

## 8. API Endpoints Specification

Mounted under `/v1` in `backend/api/main.py`:

| Method | Path | Summary | Expected Status Codes |
|---|---|---|---|
| `GET` | `/v1/evaluations` | List candidate evaluation manifests (cursor paginated, filter by `lifecycle_state`) | `200 OK`, `401 Unauthorized` |
| `POST` | `/v1/evaluations` | Register a candidate evaluation manifest with provenance and holdout checks | `201 Created`, `400 Bad Request`, `401 Unauthorized`, `422 Unprocessable` |
| `GET` | `/v1/evaluations/{id}` | Retrieve manifest metadata, trial progress counts, and latest report | `200 OK`, `401 Unauthorized`, `404 Not Found` |
| `POST` | `/v1/evaluations/{id}/freeze` | Freeze manifest attributes and compute cryptographic SHA-256 digest | `200 OK`, `401 Unauthorized`, `404 Not Found`, `409 Conflict` |
| `POST` | `/v1/evaluations/{id}/cancel` | Cancel an active or frozen evaluation run | `200 OK`, `401 Unauthorized`, `404 Not Found`, `409 Conflict` |
| `GET` | `/v1/evaluations/{id}/trials` | Keyset-paginated trials with filters (`arm`, `trial_status`, `cursor`, `limit`) | `200 OK`, `401 Unauthorized`, `404 Not Found` |
| `POST` | `/v1/evaluations/{id}/run` | Execute paired evaluation runner with double-blind fixtures and worker leasing | `200 OK`, `401 Unauthorized`, `404 Not Found`, `409 Conflict` |
| `POST` | `/v1/evaluations/{id}/review` | Submit human reviewer evaluation decision with CAS optimistic concurrency | `200 OK`, `401 Unauthorized`, `403 Forbidden`, `404 Not Found`, `409 Conflict` |

---

## 9. Verification & Acceptance Criteria

1. **Pre-Migration Backups:** Dumps of `opsblaze` and `opsblaze_test` verified in `.local/backups/stage9/`.
2. **Schema & Regression Safety:** Migration `stage9_001_e5` runs cleanly on both databases without modifying or truncating any historical data across Stages 1–8.
3. **Statistical Soundness:** McNemar exact binomial, Newcombe Method 10 paired CI, non-inferiority margins, zero discordant pairs, and division-by-zero safeguards verified via unit tests.
4. **Holdout Contamination Gate (`INV-E5-11`):** Verified that candidate manifests cannot reuse source incidents from the holdout fixture set.
5. **Double-Blind Execution:** Runner strips `is_negative_control` from investigator agent context; negative control prompt injections trigger `GATE_SAFETY` failures.
6. **CAS Concurrency & Dynamic Invalidation:** Concurrent reviewer submissions produce HTTP 409 Conflict; disputed outcome revisions or revoked tenant consent dynamically invalidate report promotion eligibility.
7. **Complete Verification:** All existing 194 tests pass alongside new Stage 9 unit, integration, and chaos test suites.
